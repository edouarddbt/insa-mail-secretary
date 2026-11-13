"""Outils IMAP multi-dossiers (lecture seule par défaut, déplacement explicite)."""

from __future__ import annotations

import email
import email.policy
import logging
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from imapclient import IMAPClient

from src.config import Settings
from src.email_parser import ParsedEmail, parse_raw_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FolderInfo:
    """Représente un dossier IMAP et son rôle détecté."""

    name: str
    role: str


def to_ascii_term(text: str) -> str:
    """Supprime les accents pour compatibilité maximale avec IMAP SEARCH."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_only.split()).strip()


def _extract_body_bytes(data: dict[object, object] | None) -> bytes | None:
    """Extrait les octets d'un FETCH IMAP complet (BODY[]) ou partiel (BODY[]<0>)."""
    if not data:
        return None
    direct = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
    if isinstance(direct, bytes):
        return direct
    for key, val in data.items():
        if isinstance(key, bytes) and key.startswith(b"BODY[") and isinstance(val, bytes):
            return val
    return None


def _detect_folder_role(flags: tuple[bytes, ...], name: str) -> str:
    flag_set = {f.lower() for f in flags}
    if b"\\trash" in flag_set:
        return "trash"
    if b"\\junk" in flag_set:
        return "junk"
    if b"\\sent" in flag_set:
        return "sent"
    if b"\\archive" in flag_set:
        return "archive"
    if b"\\drafts" in flag_set:
        return "drafts"

    lower = to_ascii_term(name).lower()
    if lower == "inbox":
        return "inbox"
    if any(k in lower for k in ("trash", "corbeille", "deleted")):
        return "trash"
    if any(k in lower for k in ("junk", "spam", "pourriel")):
        return "junk"
    if any(k in lower for k in ("sent", "envoye")):
        return "sent"
    if "archive" in lower:
        return "archive"
    if any(k in lower for k in ("draft", "brouillon")):
        return "drafts"
    return "other"


class ImapMailboxTools:
    """Fournit des opérations IMAP atomiques et sûres sur l'ensemble des dossiers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _connect(self) -> IMAPClient:
        client = IMAPClient(
            self.settings.imap_host,
            port=self.settings.imap_port,
            ssl=True,
            timeout=30,
        )
        client.login(self.settings.imap_user, self.settings.imap_password)
        return client

    def list_folders(self) -> list[FolderInfo]:
        """Liste tous les dossiers de la boîte mail avec leur rôle (inbox, trash, etc.)."""
        client = self._connect()
        try:
            folders: list[FolderInfo] = []
            for flags, _delim, name in client.list_folders():
                if b"\\Noselect" in {f.lower() for f in flags}:
                    continue
                folder_name = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
                folders.append(
                    FolderInfo(
                        name=folder_name,
                        role=_detect_folder_role(tuple(flags), folder_name),
                    )
                )
            return folders
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def search_folder(
        self,
        folder: str,
        keywords: list[str] | None = None,
        sender: str | None = None,
        days_back: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        unseen_only: bool = False,
        max_results: int = 25,
    ) -> list[ParsedEmail]:
        """Recherche des mails dans un dossier précis (lecture seule, reste non lu)."""
        client = self._connect()
        try:
            client.select_folder(folder, readonly=True)

            today_local = datetime.now(self.settings.timezone).date()
            base_criteria: list[object] = []
            if unseen_only:
                base_criteria.append("UNSEEN")
            if date_from:
                try:
                    parsed_from = datetime.fromisoformat(date_from).date()
                    if parsed_from <= today_local:
                        base_criteria.extend(["SENTSINCE", parsed_from])
                except ValueError:
                    pass
            elif days_back and days_back > 0:
                since_date = today_local - timedelta(days=days_back)
                base_criteria.extend(["SENTSINCE", since_date])

            if date_to and "SENTSINCE" in base_criteria:
                try:
                    # IMAP SENTBEFORE est exclusif, on ajoute +1 jour pour inclure date_to
                    before_date = datetime.fromisoformat(date_to).date() + timedelta(days=1)
                    base_criteria.extend(["SENTBEFORE", before_date])
                except ValueError:
                    pass

            clean_keywords: list[str] = []
            seen_kw: set[str] = set()
            for raw_kw in keywords or []:
                term = to_ascii_term(raw_kw)
                key = term.lower()
                if len(term) >= 2 and key not in seen_kw:
                    seen_kw.add(key)
                    clean_keywords.append(term)
            clean_sender = to_ascii_term(sender) if sender else ""

            uid_scores: dict[int, int] = {}

            def _add_hits(uids_iter: object, weight: int = 1) -> None:
                for raw_u in uids_iter:  # type: ignore[union-attr]
                    u = int(raw_u)
                    uid_scores[u] = uid_scores.get(u, 0) + weight

            if clean_sender:
                _add_hits(
                    client.search([*base_criteria, "FROM", clean_sender]), weight=2
                )
            for kw in clean_keywords[:8]:
                weight = 2 if len(kw) >= 5 else 1
                _add_hits(
                    client.search([*base_criteria, "TEXT", kw]), weight=weight
                )

            if not clean_sender and not clean_keywords:
                _add_hits(client.search(base_criteria or ["ALL"]), weight=1)
            elif not uid_scores and (days_back or date_from or date_to) and (
                clean_sender or clean_keywords
            ):
                # Repli sans filtre de date si la fenêtre de date + mot-clé était trop stricte
                fallback_base = ["UNSEEN"] if unseen_only else []
                if clean_sender:
                    _add_hits(
                        client.search([*fallback_base, "FROM", clean_sender]), weight=2
                    )
                for kw in clean_keywords[:8]:
                    weight = 2 if len(kw) >= 5 else 1
                    _add_hits(
                        client.search([*fallback_base, "TEXT", kw]), weight=weight
                    )

            if not uid_scores:
                return []

            # Priorise les mails correspondant au plus grand nombre de mots-clés,
            # puis trie par vraie date d'envoi (ENVELOPE.date) décroissante.
            ranked_pool = sorted(
                uid_scores.keys(), key=lambda u: (uid_scores[u], u)
            )[-300:]
            ordered_uids = self._sort_uids_newest_first(
                client, ranked_uids=ranked_pool, scores=uid_scores
            )
            target_uids = ordered_uids[:max_results]

            # FETCH partiel (64 Ko max par mail) + sans extraction PDF pour éviter
            # tout pic mémoire/OOM sur les mails contenant de grosses pièces jointes.
            fetched = client.fetch(target_uids, ["BODY.PEEK[]<0.65536>"])
            results: list[ParsedEmail] = []
            for uid in target_uids:
                raw_bytes = _extract_body_bytes(fetched.get(uid))
                if raw_bytes is not None:
                    results.append(
                        parse_raw_email(uid, raw_bytes, extract_pdfs=False)
                    )
            return results
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def read_emails_by_uids(self, folder: str, uids: list[int]) -> list[ParsedEmail]:
        """Lit en détail une liste précise d'UID dans un dossier (lecture seule, bornée en RAM)."""
        if not uids:
            return []
        client = self._connect()
        try:
            client.select_folder(folder, readonly=True)
            results: list[ParsedEmail] = []
            for uid in uids[:5]:
                fetched = client.fetch([uid], ["BODY.PEEK[]<0.6291456>"])
                raw_bytes = _extract_body_bytes(fetched.get(uid))
                if raw_bytes is not None:
                    results.append(parse_raw_email(uid, raw_bytes))
            return results
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def move_emails(
        self, source_folder: str, target_folder: str, uids: list[int]
    ) -> int:
        """Déplace explicitement des UID d'un dossier vers un autre."""
        if not uids:
            return 0
        client = self._connect()
        try:
            client.select_folder(source_folder, readonly=False)
            if client.has_capability(b"MOVE"):
                client.move(uids, target_folder)
            else:
                client.copy(uids, target_folder)
                client.delete_messages(uids)
                client.expunge()
            return len(uids)
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def find_folder_by_role(self, role: str) -> str | None:
        """Retourne le nom réel du premier dossier correspondant au rôle demandé."""
        for folder in self.list_folders():
            if folder.role == role:
                return folder.name
        return None

    def ensure_archive_folder(self, preferred_name: str = "") -> str:
        """Retourne (et crée si absent) le dossier Archive de la boîte mail."""
        folders = self.list_folders()
        if preferred_name:
            for folder in folders:
                if folder.name.lower() == preferred_name.lower():
                    return folder.name
            target = preferred_name
        else:
            for folder in folders:
                if folder.role == "archive":
                    return folder.name
            target = "Archive"

        client = self._connect()
        try:
            if not client.folder_exists(target):
                client.create_folder(target)
                logger.info("Dossier IMAP créé : %s", target)
            return target
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def mark_seen(self, folder: str, uids: list[int], seen: bool = True) -> None:
        """Ajoute ou retire le drapeau \\Seen sur une liste d'UID."""
        if not uids:
            return
        client = self._connect()
        try:
            client.select_folder(folder, readonly=False)
            if seen:
                client.add_flags(uids, [b"\\Seen"])
            else:
                client.remove_flags(uids, [b"\\Seen"])
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def fetch_sent_reply_metadata(
        self, sent_folder: str, days_back: int = 6, max_results: int = 50
    ) -> list[dict[str, object]]:
        """Récupère les en-têtes de réponse des mails envoyés récents."""
        client = self._connect()
        try:
            client.select_folder(sent_folder, readonly=True)
            since_date = (
                datetime.now(self.settings.timezone) - timedelta(days=days_back)
            ).date()
            uids = sorted(int(u) for u in client.search(["SENTSINCE", since_date]))
            if not uids:
                return []
            target_uids = uids[-max_results:]
            fetched = client.fetch(
                target_uids,
                ["BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES SUBJECT TO)]"],
            )
            items: list[dict[str, object]] = []
            for uid in target_uids:
                data = fetched.get(uid) or {}
                raw_hdr = next(
                    (v for k, v in data.items() if isinstance(k, bytes) and b"HEADER" in k and isinstance(v, bytes)),
                    b"",
                )
                if not raw_hdr:
                    continue
                msg = email.message_from_bytes(raw_hdr, policy=email.policy.default)
                refs_raw = str(msg.get("References") or "")
                items.append(
                    {
                        "in_reply_to": str(msg.get("In-Reply-To") or "").strip(),
                        "references": [r.strip() for r in refs_raw.split() if r.strip()],
                        "subject": str(msg.get("Subject") or "").strip(),
                        "to": str(msg.get("To") or "").lower(),
                    }
                )
            return items
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def fetch_recent_emails_since(
        self,
        folder: str,
        since_date: date,
        skip_uid: Callable[[int, int], bool] | None = None,
        max_results: int = 50,
    ) -> tuple[int, list[ParsedEmail]]:
        """Récupère en lecture seule les mails non encore traités envoyés depuis `since_date`."""
        client = self._connect()
        try:
            select_info = client.select_folder(folder, readonly=True)
            uidvalidity = int(select_info[b"UIDVALIDITY"])
            # SENTSINCE filtre sur l'en-tête Date: réel du mail (évite les vieux mails restaurés)
            all_uids = sorted(int(u) for u in client.search(["SENTSINCE", since_date]))
            if not all_uids:
                return uidvalidity, []

            candidate_uids = all_uids[-max_results:]
            if skip_uid is not None:
                candidate_uids = [
                    uid for uid in candidate_uids if not skip_uid(uidvalidity, uid)
                ]
            if not candidate_uids:
                return uidvalidity, []

            results: list[ParsedEmail] = []
            for uid in candidate_uids:
                fetched = client.fetch([uid], ["BODY.PEEK[]<0.6291456>"])
                raw_bytes = _extract_body_bytes(fetched.get(uid))
                if raw_bytes is not None:
                    results.append(parse_raw_email(uid, raw_bytes))
            return uidvalidity, results
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _sort_uids_newest_first(
        self,
        client: IMAPClient,
        ranked_uids: list[int],
        scores: dict[int, int] | None = None,
    ) -> list[int]:
        """Trie les UIDs par pertinence (score mot-clé) puis ENVELOPE.date décroissante."""
        if len(ranked_uids) <= 1:
            return list(reversed(ranked_uids))
        try:
            env_map = client.fetch(ranked_uids, ["ENVELOPE"])
        except Exception:
            return list(reversed(ranked_uids))

        has_any_envelope = any(
            isinstance(env_map.get(uid), dict) and env_map[uid].get(b"ENVELOPE") is not None
            for uid in ranked_uids
        )
        if not has_any_envelope:
            if scores:
                return sorted(
                    ranked_uids, key=lambda u: (scores.get(u, 1), u), reverse=True
                )
            return list(reversed(ranked_uids))

        def sort_key(uid: int) -> tuple[int, float, int]:
            score = scores.get(uid, 1) if scores else 1
            data = env_map.get(uid) or {}
            env = data.get(b"ENVELOPE")
            dt = getattr(env, "date", None) if env is not None else None
            if isinstance(dt, datetime):
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=self.settings.timezone)
                return (score, dt.astimezone(timezone.utc).timestamp(), uid)
            return (score, 0.0, uid)

        return sorted(ranked_uids, key=sort_key, reverse=True)
