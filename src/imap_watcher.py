"""Surveillance IMAP en temps réel (IDLE + repli périodique) et déclenchement du récap."""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from imapclient import IMAPClient

from src.classifier import MailClassifier
from src.config import Settings
from src.email_parser import parse_raw_email
from src.imap_tools import ImapMailboxTools
from src.notifier import TelegramNotifier
from src.storage import StateStore

logger = logging.getLogger(__name__)


class ImapSecretary:
    """Orchestre la lecture IMAP non intrusive, le tri IA et le récap journalier."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        classifier: MailClassifier,
        notifier: TelegramNotifier,
        mailbox: ImapMailboxTools | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.classifier = classifier
        self.notifier = notifier
        self.mailbox = mailbox or ImapMailboxTools(settings)
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def _connect(self) -> tuple[IMAPClient, int]:
        client = IMAPClient(
            self.settings.imap_host,
            port=self.settings.imap_port,
            ssl=True,
            timeout=30,
        )
        client.login(self.settings.imap_user, self.settings.imap_password)
        select_info = client.select_folder(self.settings.imap_folder, readonly=True)
        uidvalidity = int(select_info[b"UIDVALIDITY"])
        return client, uidvalidity

    def _fetch_candidate_uids(
        self, client: IMAPClient, uidvalidity: int
    ) -> list[int]:
        last_uid = self.store.get_last_uid(uidvalidity)
        all_uids: list[int] = sorted(int(u) for u in client.search(["ALL"]))

        if last_uid is None:
            if self.settings.initial_lookback_hours <= 0:
                max_uid = max(all_uids) if all_uids else 0
                self.store.set_last_uid(uidvalidity, max_uid)
                logger.info(
                    "Initialisation premier lancement : point de départ fixé à UID=%d",
                    max_uid,
                )
                return []

            since_date = (
                datetime.now(self.settings.timezone)
                - timedelta(hours=self.settings.initial_lookback_hours)
            ).date()
            recent_uids = sorted(int(u) for u in client.search(["SINCE", since_date]))
            return recent_uids

        return [uid for uid in all_uids if uid > last_uid]

    def process_new_messages(self, client: IMAPClient, uidvalidity: int) -> int:
        """Récupère et traite tous les nouveaux UID sans modifier le statut lu/non-lu."""
        candidate_uids = self._fetch_candidate_uids(client, uidvalidity)
        if not candidate_uids:
            return 0

        processed_count = 0
        # BODY.PEEK[] préserve strictement le drapeau \Seen (mail reste non lu)
        fetched = client.fetch(candidate_uids, ["BODY.PEEK[]"])
        for uid in candidate_uids:
            data = fetched.get(uid)
            if not data:
                continue
            raw_bytes = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
            if not isinstance(raw_bytes, bytes):
                continue

            mail = parse_raw_email(uid, raw_bytes)
            if self.store.is_already_processed(uidvalidity, uid, mail.message_id):
                self.store.set_last_uid(uidvalidity, uid)
                continue

            if self._is_too_old(mail.date_iso):
                logger.info(
                    "UID=%d ignoré car trop ancien (%s) — probablement restauré/déplacé.",
                    uid,
                    mail.date_iso,
                )
                self.store.record_email(
                    uidvalidity=uidvalidity,
                    uid=uid,
                    message_id=mail.message_id,
                    sender=mail.sender_display,
                    sender_email=mail.sender_email,
                    subject=mail.subject,
                    received_at=mail.date_iso,
                    category="archived",
                    short_title=mail.subject[:80],
                    summary="Mail ancien restauré ou déplacé.",
                    action_required=None,
                    current_folder=self.settings.imap_folder,
                    is_direct_or_action=False,
                    notified_now=False,
                    digested=True,
                )
                self.store.set_last_uid(uidvalidity, uid)
                continue

            extra_muted = self.store.get_muted_senders()
            classification = self.classifier.classify(mail, extra_muted=extra_muted)
            logger.info(
                "UID=%d | %s | cat=%s | notify=%s | %s",
                uid,
                mail.sender_email,
                classification.category,
                classification.notify_now,
                classification.short_title,
            )

            notified_ok = False
            if classification.notify_now:
                try:
                    self.notifier.send_urgent_alert(
                        mail, classification, tz=self.settings.timezone
                    )
                    notified_ok = True
                except Exception as exc:
                    logger.exception(
                        "Échec envoi alerte Telegram pour UID=%d (%s) — mail enregistré pour éviter une boucle Gemini: %s",
                        uid,
                        mail.subject,
                        exc,
                    )

            current_folder = self.settings.imap_folder

            is_direct_or_action = (
                classification.category in {"urgent", "important"}
                and not mail.is_bulk_list
                and bool(mail.sender_email)
                and mail.sender_email.lower() != self.settings.imap_user.lower()
            )

            self.store.record_email(
                uidvalidity=uidvalidity,
                uid=uid,
                message_id=mail.message_id,
                sender=mail.sender_display,
                sender_email=mail.sender_email,
                subject=mail.subject,
                received_at=mail.date_iso,
                category=classification.category,
                short_title=classification.short_title,
                summary=classification.summary,
                action_required=classification.action_required,
                current_folder=current_folder,
                is_direct_or_action=is_direct_or_action,
                notified_now=notified_ok,
                digested=notified_ok,
            )
            self.store.set_last_uid(uidvalidity, uid)
            processed_count += 1

        return processed_count

    def _parse_iso_datetime(self, date_iso: str) -> datetime | None:
        if not date_iso:
            return None
        try:
            dt = datetime.fromisoformat(date_iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.settings.timezone)
        return dt

    def _is_too_old(self, date_iso: str) -> bool:
        max_hours = self.settings.max_email_age_hours
        if max_hours <= 0:
            return False
        dt = self._parse_iso_datetime(date_iso)
        if dt is None:
            return False
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age > timedelta(hours=max_hours)

    def _backfill_since_yesterday(self) -> int:
        """Récupère sans alerte push les mails non encore indexés depuis J-N."""
        days = self.settings.digest_lookback_days
        if days <= 0:
            return 0

        now_local = datetime.now(self.settings.timezone)
        cutoff_local = (now_local - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        folders = [self.settings.imap_folder]
        try:
            existing = {
                f.name.lower(): f.name for f in self.mailbox.list_folders()
            }
            archive_pref = self.settings.archive_folder.strip().lower()
            archive_actual = existing.get(archive_pref) if archive_pref else None
            if not archive_actual:
                archive_role = self.mailbox.find_folder_by_role("archive")
                if archive_role and archive_role.lower() in existing:
                    archive_actual = existing[archive_role.lower()]
            if archive_actual and all(
                archive_actual.lower() != f.lower() for f in folders
            ):
                folders.append(archive_actual)
        except Exception:
            pass

        extra_muted = self.store.get_muted_senders()
        added = 0
        for folder in folders:
            try:
                uidvalidity, mails = self.mailbox.fetch_recent_emails_since(
                    folder,
                    cutoff_local.date(),
                    skip_uid=lambda uv, u: self.store.is_already_processed(uv, u, None),
                )
            except Exception as exc:
                logger.warning("Rattrapage récap impossible sur %s: %s", folder, exc)
                continue

            for mail in mails:
                if self.store.is_already_processed(uidvalidity, mail.uid, mail.message_id):
                    continue
                mail_dt = self._parse_iso_datetime(mail.date_iso)
                if mail_dt is not None and (
                    mail_dt.astimezone(timezone.utc) < cutoff_local.astimezone(timezone.utc)
                ):
                    self.store.record_email(
                        uidvalidity=uidvalidity,
                        uid=mail.uid,
                        message_id=mail.message_id,
                        sender=mail.sender_display,
                        sender_email=mail.sender_email,
                        subject=mail.subject,
                        received_at=mail.date_iso,
                        category="archived",
                        short_title=mail.subject[:80],
                        summary="Mail antérieur à la fenêtre du récap.",
                        action_required=None,
                        current_folder=folder,
                        is_direct_or_action=False,
                        notified_now=False,
                        digested=True,
                    )
                    continue

                classification = self.classifier.classify(mail, extra_muted=extra_muted)
                is_direct_or_action = (
                    classification.category in {"urgent", "important"}
                    and not mail.is_bulk_list
                    and bool(mail.sender_email)
                    and mail.sender_email.lower() != self.settings.imap_user.lower()
                )
                self.store.record_email(
                    uidvalidity=uidvalidity,
                    uid=mail.uid,
                    message_id=mail.message_id,
                    sender=mail.sender_display,
                    sender_email=mail.sender_email,
                    subject=mail.subject,
                    received_at=mail.date_iso,
                    category=classification.category,
                    short_title=classification.short_title,
                    summary=classification.summary,
                    action_required=classification.action_required,
                    current_folder=folder,
                    is_direct_or_action=is_direct_or_action,
                    notified_now=False,
                    digested=False,
                )
                added += 1

        if added:
            logger.info("Rattrapage récap : %d mail(s) depuis J-%d indexé(s).", added, days)
        return added

    def maybe_send_daily_digest(self, force: bool = False) -> bool:
        """Envoie le récap du soir à l'heure prévue, ou un aperçu anticipé si force=True."""
        now_local = datetime.now(self.settings.timezone)
        today_str = now_local.date().isoformat()

        if not force:
            target = now_local.replace(
                hour=self.settings.digest_hour,
                minute=self.settings.digest_minute,
                second=0,
                microsecond=0,
            )
            if now_local < target:
                return False
            if self.store.get_last_digest_date() == today_str:
                return False

        self._backfill_since_yesterday()
        if force:
            lookback = max(1, self.settings.digest_lookback_days)
            since_iso = (
                (now_local - timedelta(days=lookback))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .isoformat()
            )
            items = self.store.get_undigested_newsletters(
                include_since_iso=since_iso
            )
        else:
            items = self.store.get_undigested_newsletters()
        if not items:
            if force or self.settings.digest_send_when_empty:
                msg = (
                    "Aucun mail reçu depuis hier en attente du récap de ce soir."
                    if force
                    else "Aucune newsletter ou mail secondaire reçu depuis hier."
                )
                if force:
                    self.notifier.send_daily_digest(0, msg, preview=True)
                else:
                    self.notifier.send_daily_digest(0, msg)
            if not force:
                self.store.set_last_digest_date(today_str)
            return False

        digest_text = self.classifier.summarize_digest(items)
        if force:
            self.notifier.send_daily_digest(len(items), digest_text, preview=True)
        else:
            self.notifier.send_daily_digest(len(items), digest_text)

        if not force:
            self.store.mark_digested([item.id for item in items])
            self.store.set_last_digest_date(today_str)
        logger.info(
            "%s envoyé (%d mails).",
            "Aperçu /recap" if force else "Récap du soir",
            len(items),
        )
        return True

    def run_once(self) -> int:
        """Exécute un cycle unique de synchronisation puis vérifie le récap."""
        client, uidvalidity = self._connect()
        try:
            count = self.process_new_messages(client, uidvalidity)
        finally:
            try:
                client.logout()
            except Exception:
                pass
        self.maybe_send_daily_digest(force=False)
        return count

    def _next_idle_timeout(self) -> int:
        """Calcule le délai d'IDLE pour se réveiller pile à l'heure du récap du soir."""
        base = max(10, self.settings.idle_timeout_seconds)
        now_local = datetime.now(self.settings.timezone)
        target = now_local.replace(
            hour=self.settings.digest_hour,
            minute=self.settings.digest_minute,
            second=2,
            microsecond=0,
        )
        if now_local < target and self.store.get_last_digest_date() != now_local.date().isoformat():
            seconds_until = int((target - now_local).total_seconds()) + 1
            return max(5, min(base, seconds_until))
        return base

    def run_forever(self) -> None:
        """Boucle principale 24/7 avec IMAP IDLE et reconnexion automatique."""
        signal.signal(signal.SIGTERM, lambda *_: self.request_stop())
        signal.signal(signal.SIGINT, lambda *_: self.request_stop())

        backoff = 5
        while not self._stop_requested:
            client: IMAPClient | None = None
            try:
                client, uidvalidity = self._connect()
                logger.info(
                    "Connecté en IMAP SSL à %s:%d (dossier=%s, UIDVALIDITY=%d)",
                    self.settings.imap_host,
                    self.settings.imap_port,
                    self.settings.imap_folder,
                    uidvalidity,
                )
                backoff = 5

                supports_idle = client.has_capability(b"IDLE")
                while not self._stop_requested:
                    self.process_new_messages(client, uidvalidity)
                    self.maybe_send_daily_digest(force=False)

                    if supports_idle:
                        wait_seconds = self._next_idle_timeout()
                        client.idle()
                        try:
                            remaining = wait_seconds
                            while remaining > 0 and not self._stop_requested:
                                step = min(2, remaining)
                                responses = client.idle_check(timeout=step)
                                remaining -= step
                                if responses:
                                    break
                        finally:
                            try:
                                client.idle_done()
                            except Exception:
                                pass
                    else:
                        remaining = max(1, self.settings.poll_fallback_seconds)
                        while remaining > 0 and not self._stop_requested:
                            time.sleep(min(1, remaining))
                            remaining -= 1
                        if not self._stop_requested:
                            client.noop()

            except Exception as exc:
                if self._stop_requested:
                    break
                logger.warning(
                    "Connexion IMAP interrompue (%s). Reconnexion dans %ds...",
                    exc,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
            finally:
                if client is not None:
                    try:
                        client.logout()
                    except Exception:
                        pass
