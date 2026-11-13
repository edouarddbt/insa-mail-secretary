"""Assistant conversationnel agentique Telegram (multi-dossiers + actions confirmées)."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from google.genai import types

from src.classifier import MailClassifier
from src.config import Settings
from src.email_parser import ParsedEmail
from src.imap_tools import ImapMailboxTools
from src.notifier import TelegramNotifier
from src.storage import StateStore
from src.system_info import collect_system_metrics, format_vps_status

logger = logging.getLogger(__name__)

MAX_TOOL_TURNS = 8
CONFIRM_TTL_SECONDS = 600
CHAT_HISTORY_TTL_SECONDS = 900
CHAT_HISTORY_MAX_TURNS = 6


@dataclass
class PendingMoveAction:
    """Action de déplacement de mails en attente de confirmation utilisateur."""

    action_id: str
    source_folder: str
    target_folder: str
    uids: list[int]
    preview_lines: list[str]
    reason: str
    created_at: float

    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > CONFIRM_TTL_SECONDS


class MailSearchService:
    """Agent IA disposant d'outils IMAP pour chercher, lire et proposer des déplacements."""

    def __init__(
        self,
        settings: Settings,
        classifier: MailClassifier,
        mailbox: ImapMailboxTools | None = None,
    ) -> None:
        self.settings = settings
        self.classifier = classifier
        self.mailbox = mailbox or ImapMailboxTools(settings)

    def run_agent(
        self,
        question: str,
        conversation_history: list[tuple[str, str]] | None = None,
    ) -> tuple[str, PendingMoveAction | None]:
        """Exécute la boucle d'outils Gemini et retourne (réponse, action_en_attente)."""
        proposed_action: PendingMoveAction | None = None

        def list_mail_folders() -> dict[str, Any]:
            """Liste tous les dossiers IMAP disponibles (INBOX, Trash/Corbeille, Junk/Spam, Sent, etc.)."""
            folders = self.mailbox.list_folders()
            return {
                "folders": [{"name": f.name, "role": f.role} for f in folders]
            }

        def search_emails(
            folder: str = "INBOX",
            keywords: list[str] | None = None,
            sender: str | None = None,
            days_back: int | None = None,
            date_from: str | None = None,
            date_to: str | None = None,
            unseen_only: bool = False,
            max_results: int = 25,
        ) -> dict[str, Any]:
            """Recherche des mails dans un dossier IMAP précis (ex: INBOX, Trash, Sent, Junk) en lecture seule. date_from/date_to au format YYYY-MM-DD."""
            bounded = max(1, min(max_results, 40))
            emails = self.mailbox.search_folder(
                folder=folder,
                keywords=keywords,
                sender=sender,
                days_back=days_back,
                date_from=date_from,
                date_to=date_to,
                unseen_only=unseen_only,
                max_results=bounded,
            )
            items_out: list[dict[str, Any]] = []
            for idx, m in enumerate(emails):
                body = m.body_text or ""
                # Donne jusqu'à 2200 caractères sur les 8 meilleurs résultats
                # pour que les conditions en bas de mail soient visibles d'emblée.
                limit = 2200 if idx < 8 else 600
                truncated = len(body) > limit
                entry: dict[str, Any] = {
                    "uid": m.uid,
                    "date": m.date_iso,
                    "from": m.sender_display,
                    "subject": m.subject,
                    "is_bulk_list": m.is_bulk_list,
                    "snippet": body[:limit],
                    "is_truncated": truncated,
                    "total_chars": len(body),
                }
                if truncated:
                    entry["warning"] = (
                        "Corps tronqué : appelle obligatoirement read_email_detail "
                        "avant d'affirmer qu'une info est absente de ce mail."
                    )
                items_out.append(entry)
            return {
                "folder": folder,
                "count": len(emails),
                "emails": items_out,
            }

        def read_email_detail(folder: str, uid: int) -> dict[str, Any]:
            """Lit le contenu complet d'un mail identifié par son dossier et son UID."""
            found = self.mailbox.read_emails_by_uids(folder, [uid])
            if not found:
                return {"found": False, "error": f"Mail UID={uid} introuvable dans {folder}."}
            mail = found[0]
            return {
                "found": True,
                "uid": mail.uid,
                "folder": folder,
                "date": mail.date_iso,
                "from": mail.sender_display,
                "to": list(mail.to_addrs),
                "subject": mail.subject,
                "body": mail.body_text,
            }

        def propose_move_emails(
            source_folder: str,
            target_folder: str,
            uids: list[int],
            reason: str,
        ) -> dict[str, Any]:
            """Prépare le déplacement de mails (ex: de Trash vers INBOX) pour confirmation utilisateur. N'exécute PAS immédiatement."""
            nonlocal proposed_action
            clean_uids = [int(u) for u in uids if int(u) > 0][:30]
            if not clean_uids:
                return {"queued": False, "error": "Aucun UID valide fourni."}
            mails = self.mailbox.read_emails_by_uids(source_folder, clean_uids)
            if not mails:
                return {
                    "queued": False,
                    "error": f"Aucun de ces UIDs trouvé dans {source_folder}.",
                }
            verified_uids = [m.uid for m in mails]
            previews = [
                f"[UID {m.uid}] {m.sender_display} — {m.subject}" for m in mails
            ]
            proposed_action = PendingMoveAction(
                action_id=secrets.token_hex(4),
                source_folder=source_folder,
                target_folder=target_folder,
                uids=verified_uids,
                preview_lines=previews,
                reason=reason,
                created_at=time.monotonic(),
            )
            return {
                "queued": True,
                "count": len(verified_uids),
                "source_folder": source_folder,
                "target_folder": target_folder,
                "message": "Demande prête. L'utilisateur devra cliquer sur Confirmer dans Telegram.",
            }

        tool_fns: dict[str, Callable[..., dict[str, Any]]] = {
            "list_mail_folders": list_mail_folders,
            "search_emails": search_emails,
            "read_email_detail": read_email_detail,
            "propose_move_emails": propose_move_emails,
        }
        declarations = [
            types.FunctionDeclaration.from_callable(
                client=self.classifier.client, callable=fn, use_json_schema=True
            )
            for fn in tool_fns.values()
        ]

        today_str = datetime.now(self.settings.timezone).date().isoformat()
        system_instruction = (
            "Tu es le Secrétaire IA personnel de l'utilisateur pour sa boîte mail INSA Rouen.\n"
            f"Date du jour : {today_str}\n"
            f"Contexte utilisateur : {self.settings.user_context}\n"
            "Tu disposes d'outils IMAP pour :\n"
            "1. Lister les dossiers (pour trouver le vrai nom de la corbeille, des spams, des envoyés, etc.).\n"
            "2. Chercher et lire des mails dans N'IMPORTE QUEL dossier (lecture seule, sans marquer lu).\n"
            "3. Proposer un déplacement de mails (ex: restaurer de la corbeille vers INBOX) via `propose_move_emails`.\n\n"
            "Règles strictes de rigueur (TRÈS IMPORTANT) :\n"
            "- Recherche large et intelligente : quand l'utilisateur cherche un événement ou une règle (ex: 'séance de cinéma obligatoire'), passe dans `keywords` À LA FOIS les adjectifs/contraintes clés ('obligatoire', 'inscription', 'examen') ET des synonymes variés ('cinema', 'projection', 'film', 'documentaire', 'conference', 'amphi'), jusqu'à 8 mots-clés sans accents.\n"
            "- Attention aux dates : `date_from` et `date_to` dans `search_emails` filtrent la DATE D'ENVOI du mail (toujours dans le passé), JAMAIS la date future d'un événement annoncé dans le mail.\n"
            "- Lecture intégrale obligatoire : lis attentivement CHAQUE mail jusqu'à la toute dernière ligne (notamment les mentions entre parenthèses comme 'HORS élèves de 4ème année, pour lesquels...'). Si un mail pertinent a `is_truncated: true`, appelle OBLIGATOIREMENT `read_email_detail(folder, uid)` avant de répondre. N'affirme JAMAIS qu'un événement est non obligatoire ou qu'une info est absente sans avoir lu la fin du mail.\n"
            "- Si la question concerne la corbeille, les spams ou les envoyés, appelle d'abord `list_mail_folders` pour connaître le nom exact du dossier.\n"
            "- Si une recherche par mot-clé retourne 0 résultat, réessaie avec d'autres synonymes ou sans filtre de date (`max_results=30`).\n"
            "- Si l'utilisateur demande de déplacer/restaurer des mails, identifie d'abord les mails concernés puis appelle `propose_move_emails` (une confirmation lui sera demandée).\n"
            "- Réponds en français, de façon aérée et très lisible sur téléphone : utilise des titres de rubriques en **gras** avec un emoji (ex: 📚 **Scolarité**), des puces `- ` courtes (1 à 2 phrases max par mail avec l'info/date clé), et saute une ligne entre chaque élément et chaque rubrique. N'utilise aucune balise HTML."
        )

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[types.Tool(function_declarations=declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            temperature=0.1,
        )

        history: list[types.Content] = []
        for prev_user, prev_assistant in conversation_history or []:
            history.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=prev_user)],
                )
            )
            history.append(
                types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=prev_assistant)],
                )
            )
        history.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=question)],
            )
        )

        for _ in range(MAX_TOOL_TURNS):
            response = self.classifier.generate_with_fallback(history, config)
            candidates = getattr(response, "candidates", None) or []
            if not candidates or not candidates[0].content:
                break

            model_content = candidates[0].content
            history.append(model_content)

            fn_calls = getattr(response, "function_calls", None) or []
            if not fn_calls:
                text = (getattr(response, "text", None) or "").strip()
                return (
                    text or "Je n'ai pas pu formuler de réponse.",
                    proposed_action,
                )

            response_parts: list[types.Part] = []
            for call in fn_calls:
                fn_name = str(call.name)
                fn_args = dict(call.args or {})
                fn = tool_fns.get(fn_name)
                if fn is None:
                    result: dict[str, Any] = {"error": f"Outil inconnu: {fn_name}"}
                else:
                    try:
                        logger.info("Outil agent %s(%s)", fn_name, fn_args)
                        result = fn(**fn_args)
                    except Exception as exc:
                        logger.exception("Erreur outil %s: %s", fn_name, exc)
                        result = {"error": str(exc)}
                response_parts.append(
                    types.Part.from_function_response(
                        name=fn_name,
                        response={"result": result},
                    )
                )

            # Gemini generateContent attend role="user" pour les function_response
            history.append(types.Content(role="user", parts=response_parts))

        return (
            "J'ai atteint la limite d'étapes pour cette recherche. Reformule ou précise un dossier/mot-clé.",
            proposed_action,
        )

    def answer_question(self, question: str) -> str:
        """Point d'entrée texte simple (ex: CLI --ask)."""
        reply, pending = self.run_agent(question)
        if pending is not None:
            lines = "\n".join(f"• {line}" for line in pending.preview_lines)
            return (
                f"{reply}\n\n[Action proposée en mode CLI — utilise Telegram pour confirmer]\n"
                f"Déplacer {len(pending.uids)} mail(s) de {pending.source_folder} -> {pending.target_folder} :\n{lines}"
            )
        return reply


class TelegramAssistantLoop:
    """Écoute messages et boutons Telegram pour piloter l'agent mail."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        notifier: TelegramNotifier,
        search_service: MailSearchService,
        trigger_digest_cb: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.search_service = search_service
        self.trigger_digest_cb = trigger_digest_cb
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_action: PendingMoveAction | None = None
        self._chat_history: list[tuple[str, str]] = []
        self._last_chat_at: float = 0.0
        self._started_at: float = time.monotonic()
        self._started_wall_time: datetime = datetime.now(settings.timezone)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="telegram-assistant", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _execute_pending(self, action: PendingMoveAction) -> None:
        if action.is_expired():
            self.notifier.send_plain_reply(
                "⏳ Cette demande de déplacement a expiré (> 10 min). Redemande-la si besoin."
            )
            return
        moved = self.search_service.mailbox.move_emails(
            action.source_folder, action.target_folder, action.uids
        )
        self.notifier.send_plain_reply(
            f"✅ {moved} mail(s) déplacé(s) de {action.source_folder} vers {action.target_folder} !"
        )

    def _candidate_folders_for_uid(self, uid: int) -> list[str]:
        """Liste ordonnée des dossiers probables où se trouve un UID donné."""
        folders: list[str] = []
        record = self.store.get_email_by_uid(uid)
        if record and record.get("current_folder"):
            folders.append(str(record["current_folder"]))
        if self.settings.imap_folder not in folders:
            folders.append(self.settings.imap_folder)
        if self.settings.archive_folder and self.settings.archive_folder not in folders:
            folders.append(self.settings.archive_folder)
        return folders

    def _locate_mail_by_uid(self, uid: int) -> tuple[str, ParsedEmail] | None:
        """Retrouve et lit un mail par UID sur le serveur IMAP (lecture seule)."""
        for folder in self._candidate_folders_for_uid(uid):
            try:
                found = self.search_service.mailbox.read_emails_by_uids(folder, [uid])
            except Exception:
                continue
            if found:
                return folder, found[0]
        return None

    def _handle_uid_action(self, cb_id: str, verb: str, uid_raw: str) -> None:
        """Exécute les actions rapides déclenchées sous une alerte Telegram."""
        try:
            uid = int(uid_raw)
        except ValueError:
            self.notifier.answer_callback_query(cb_id, "Identifiant de mail invalide.")
            return

        if verb == "mute":
            record = self.store.get_email_by_uid(uid)
            sender_email = str((record or {}).get("sender_email") or "").strip()
            if not sender_email:
                located = self._locate_mail_by_uid(uid)
                sender_email = located[1].sender_email if located else ""
            if not sender_email:
                self.notifier.answer_callback_query(cb_id, "Expéditeur introuvable.")
                return
            self.store.add_muted_sender(sender_email)
            self.notifier.answer_callback_query(cb_id, "Expéditeur mis en silencieux 🔕")
            self.notifier.send_plain_reply(
                f"🔕 **{sender_email}** ajouté aux expéditeurs silencieux.\n"
                "Ses prochains mails iront directement au récap du soir."
            )
            return

        if verb == "read":
            self.notifier.answer_callback_query(cb_id, "Lecture du mail...")
            located = self._locate_mail_by_uid(uid)
            if not located:
                self.notifier.send_plain_reply(
                    "❌ Mail introuvable sur le serveur (peut-être déjà déplacé ou supprimé)."
                )
                return
            _, mail = located
            parts = [
                f"📖 **{mail.subject}**",
                f"**De :** {mail.sender_display}",
                f"**Date :** {mail.date_iso}",
            ]
            if mail.attachment_names:
                parts.append(f"**Pièces jointes :** {', '.join(mail.attachment_names)}")
            parts.append("")
            parts.append((mail.body_text or "(Corps vide)")[:3300])
            self.notifier.send_plain_reply("\n".join(parts))
            return

        if verb == "reply":
            self.notifier.answer_callback_query(cb_id, "Rédaction du brouillon...")
            located = self._locate_mail_by_uid(uid)
            if not located:
                self.notifier.send_plain_reply(
                    "❌ Mail introuvable pour générer un brouillon."
                )
                return
            _, mail = located
            draft = self.search_service.classifier.draft_reply(mail)
            self.notifier.send_plain_reply(
                f"✍️ **Brouillon de réponse pour :** {mail.sender_display}\n\n{draft}"
            )
            return

        if verb == "seen":
            located = self._locate_mail_by_uid(uid)
            if not located:
                self.notifier.answer_callback_query(cb_id, "Mail introuvable.")
                return
            folder, _ = located
            self.search_service.mailbox.mark_seen(folder, [uid], seen=True)
            self.notifier.answer_callback_query(cb_id, "Marqué comme lu ✅")
            self.notifier.send_plain_reply(f"✅ Mail marqué comme lu dans **{folder}**.")
            return

        if verb == "trash":
            located = self._locate_mail_by_uid(uid)
            if not located:
                self.notifier.answer_callback_query(cb_id, "Mail introuvable.")
                return
            source_folder, mail = located
            trash_folder = self.search_service.mailbox.find_folder_by_role("trash") or "Trash"
            if source_folder.lower() != trash_folder.lower():
                self.search_service.mailbox.move_emails(source_folder, trash_folder, [uid])
                self.store.update_email_folder(uid, trash_folder)
            self.notifier.answer_callback_query(cb_id, "Déplacé dans la corbeille 🗑️")
            self.notifier.send_plain_reply(
                f"🗑️ **{mail.subject}** déplacé vers **{trash_folder}**."
            )
            return

        self.notifier.answer_callback_query(cb_id)

    def _handle_callback_query(self, callback: dict[str, object]) -> None:
        cb_id = str(callback.get("id", ""))
        message = callback.get("message")
        chat_id = ""
        if isinstance(message, dict) and isinstance(message.get("chat"), dict):
            chat_id = str(message["chat"].get("id", ""))

        if chat_id != self.settings.telegram_chat_id:
            logger.warning("Callback ignoré d'un chat non autorisé : %s", chat_id)
            return

        data = str(callback.get("data", ""))
        if ":" not in data:
            self.notifier.answer_callback_query(cb_id)
            return

        verb, payload = data.split(":", 1)
        if verb in {"read", "seen", "trash", "reply", "mute"}:
            try:
                self._handle_uid_action(cb_id, verb, payload)
            except Exception as exc:
                logger.exception("Erreur action rapide (%s:%s): %s", verb, payload, exc)
                self.notifier.answer_callback_query(cb_id, "Erreur lors de l'action.")
                self.notifier.send_plain_reply(f"❌ Erreur sur l'action `{verb}` : {exc}")
            return

        pending = self._pending_action
        if pending is None or pending.action_id != payload:
            self.notifier.answer_callback_query(cb_id, "Action expirée ou déjà traitée.")
            return

        self._pending_action = None
        if verb == "confirm":
            self.notifier.answer_callback_query(cb_id, "Déplacement en cours...")
            try:
                self._execute_pending(pending)
            except Exception as exc:
                logger.exception("Erreur lors du déplacement confirmé : %s", exc)
                self.notifier.send_plain_reply(
                    f"❌ Impossible de déplacer les mails : {exc}"
                )
        elif verb == "cancel":
            self.notifier.answer_callback_query(cb_id, "Annulé.")
            self.notifier.send_plain_reply(
                "❌ Déplacement annulé, aucun mail n'a été modifié."
            )

    def handle_update(self, update: dict[str, object]) -> None:
        """Traite un update Telegram (message texte ou clic bouton)."""
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback_query(callback)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict):
            return

        chat_id = str(chat.get("id", ""))
        if chat_id != self.settings.telegram_chat_id:
            logger.warning("Message ignoré d'un chat non autorisé : %s", chat_id)
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        clean = text.strip()
        cmd = clean.split()[0].lower()

        if cmd in {"/oui", "oui", "/confirmer", "confirmer"} and self._pending_action:
            pending = self._pending_action
            self._pending_action = None
            try:
                self._execute_pending(pending)
            except Exception as exc:
                self.notifier.send_plain_reply(f"❌ Erreur de déplacement : {exc}")
            return

        if cmd in {"/non", "non", "/annuler", "annuler"} and self._pending_action:
            self._pending_action = None
            self.notifier.send_plain_reply(
                "❌ Déplacement annulé, aucun mail n'a été modifié."
            )
            return

        if cmd in {"/start", "/help", "/aide"}:
            self.notifier.send_plain_reply(
                "👋 Je suis ton Secrétaire IA INSA Rouen.\n\n"
                "Tu peux me demander des recherches ou actions sur tous tes dossiers :\n"
                "• « Quels mails importants sont dans ma corbeille cette semaine ? »\n"
                "• « Remets les mails non-pub de ma corbeille dans ma boîte de réception »\n"
                "• « À quels mails de profs n'ai-je pas encore répondu ? »\n\n"
                "Commandes :\n"
                "/status — État du bot et mails en attente\n"
                "/recap — Aperçu anticipé du récap (sans annuler celui de 20h)\n"
                "/nouveau — Réinitialiser la conversation en cours\n"
                "/aide — Afficher ce message"
            )
            return

        if cmd in {"/reset", "/nouveau"}:
            self._chat_history.clear()
            self._pending_action = None
            self.notifier.send_plain_reply(
                "🧹 Mémoire de conversation réinitialisée !"
            )
            return

        if cmd == "/status":
            stats = self.store.get_stats()
            metrics = collect_system_metrics(self.settings.db_path)
            report = format_vps_status(
                metrics,
                bot_uptime_seconds=time.monotonic() - self._started_at,
                started_at_str=self._started_wall_time.strftime("%d/%m à %H:%M"),
                settings=self.settings,
                stats=stats,
            )
            self.notifier.send_plain_reply(report)
            return

        if cmd == "/recap":
            if self.trigger_digest_cb:
                self.trigger_digest_cb()
            else:
                self.notifier.send_plain_reply("Commande /recap indisponible dans ce mode.")
            return

        now = time.monotonic()
        if (now - self._last_chat_at) > CHAT_HISTORY_TTL_SECONDS:
            self._chat_history.clear()

        self.notifier.send_chat_action("typing")
        started = time.monotonic()
        logger.info("Question reçue sur Telegram (%d car.)", len(clean))
        try:
            answer, pending = self.search_service.run_agent(
                clean, conversation_history=list(self._chat_history)
            )
        except Exception as exc:
            logger.exception("Erreur lors de l'agent Telegram : %s", exc)
            self.notifier.send_plain_reply(
                f"Désolé, impossible d'interroger la boîte mail pour le moment ({exc})."
            )
            return
        logger.info("Réponse agent générée en %.2fs", time.monotonic() - started)

        self._chat_history.append((clean, answer))
        if len(self._chat_history) > CHAT_HISTORY_MAX_TURNS:
            self._chat_history = self._chat_history[-CHAT_HISTORY_MAX_TURNS:]
        self._last_chat_at = time.monotonic()

        self.notifier.send_plain_reply(answer)
        if pending is not None:
            self._pending_action = pending
            preview_block = "\n".join(f"• {line}" for line in pending.preview_lines[:15])
            if len(pending.preview_lines) > 15:
                preview_block += f"\n… (+{len(pending.preview_lines) - 15} autres)"
            confirm_text = (
                f"⚠️ Confirmation requise :\n"
                f"Déplacer {len(pending.uids)} mail(s) de {pending.source_folder} → {pending.target_folder}\n"
                f"Motif : {pending.reason}\n\n"
                f"{preview_block}\n\n"
                "Clique ci-dessous (ou réponds /oui ou /non) :"
            )
            self.notifier.send_confirmation_prompt(confirm_text, pending.action_id)

    def _run_loop(self) -> None:
        logger.info("Écoute conversationnelle Telegram démarrée.")
        last_id = self.store.get_last_telegram_update_id()
        offset = last_id + 1 if last_id is not None else None

        while not self._stop_event.is_set():
            try:
                updates = self.notifier.get_updates(offset=offset, timeout=8)
                for update in updates:
                    raw_uid = update.get("update_id")
                    if isinstance(raw_uid, int):
                        offset = raw_uid + 1
                        self.store.set_last_telegram_update_id(raw_uid)
                    self.handle_update(update)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("Erreur polling Telegram (%s), nouvel essai dans 5s...", exc)
                self._stop_event.wait(5)
