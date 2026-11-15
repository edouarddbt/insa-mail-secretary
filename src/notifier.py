"""Envoi des notifications et du récapitulatif journalier sur Telegram."""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from src.classifier import CalendarEvent, EmailClassification
from src.config import Settings
from src.email_parser import ParsedEmail
from src.system_info import SystemMetrics, collect_system_metrics

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4000


def build_google_calendar_url(
    event: CalendarEvent, tz: ZoneInfo | None = None
) -> str | None:
    """Construit un lien Google Agenda prérempli à partir d'un CalendarEvent."""
    title = event.title.strip()
    start_raw = event.start_iso.strip()
    if not title or not start_raw:
        return None

    local_tz = tz or ZoneInfo("Europe/Paris")
    date_only_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    try:
        if date_only_re.match(start_raw):
            start_day = datetime.strptime(start_raw, "%Y-%m-%d").date()
            end_raw = event.end_iso.strip()
            if date_only_re.match(end_raw):
                end_day = datetime.strptime(end_raw, "%Y-%m-%d").date()
                if end_day <= start_day:
                    end_day = start_day + timedelta(days=1)
            else:
                end_day = start_day + timedelta(days=1)
            dates_param = f"{start_day:%Y%m%d}/{end_day:%Y%m%d}"
        else:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=local_tz)
            end_dt: datetime | None = None
            end_raw = event.end_iso.strip()
            if end_raw:
                try:
                    parsed_end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                    if parsed_end.tzinfo is None:
                        parsed_end = parsed_end.replace(tzinfo=local_tz)
                    if parsed_end > start_dt:
                        end_dt = parsed_end
                except ValueError:
                    end_dt = None
            if end_dt is None:
                end_dt = start_dt + timedelta(hours=1)
            start_utc = start_dt.astimezone(timezone.utc)
            end_utc = end_dt.astimezone(timezone.utc)
            dates_param = f"{start_utc:%Y%m%dT%H%M%SZ}/{end_utc:%Y%m%dT%H%M%SZ}"
    except ValueError:
        logger.debug("Impossible de parser l'événement calendrier: %r", start_raw)
        return None

    params: dict[str, str] = {
        "action": "TEMPLATE",
        "text": title[:180],
        "dates": dates_param,
    }
    if event.location.strip():
        params["location"] = event.location.strip()[:180]
    if event.details.strip():
        params["details"] = event.details.strip()[:600]
    return f"https://calendar.google.com/calendar/render?{urlencode(params)}"


def markdown_to_telegram_html(text: str) -> str:
    """Convertit un texte Markdown léger (Gemini) en HTML propre pour Telegram."""
    escaped = html.escape(text)
    # Titres Markdown (# Titre) -> gras
    escaped = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", escaped)
    # Puces Markdown (* item ou - item) -> puce propre •
    escaped = re.sub(r"(?m)^([ \t]*)[*-]\s+", r"\1• ", escaped)
    # Gras **texte** ou __texte__
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
    # Italique *texte* (sur une même ligne)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", escaped)
    # Code inline `texte`
    escaped = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", escaped)
    # Aérer automatiquement entre deux puces consécutives pour lecture mobile
    lines = escaped.splitlines()
    spaced: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if (
            spaced
            and stripped.startswith("• ")
            and spaced[-1].strip() != ""
            and not spaced[-1].rstrip().endswith("</b>")
        ):
            spaced.append("")
        spaced.append(line)
    result = "\n".join(spaced)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Découpe un long message proprement aux sauts de ligne pour Telegram."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return [c for c in chunks if c]


class TelegramNotifier:
    """Client léger pour l'API Bot Telegram."""

    def __init__(self, settings: Settings, timeout_seconds: int = 15) -> None:
        self.bot_token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.timeout_seconds = timeout_seconds
        self._base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def _send_raw(
        self,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        disable_notification: bool = False,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        chunks = split_message(text)
        for idx, part in enumerate(chunks):
            payload: dict[str, object] = {
                "chat_id": self.chat_id,
                "text": part,
                "disable_notification": disable_notification,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup and idx == len(chunks) - 1:
                payload["reply_markup"] = reply_markup

            resp = requests.post(
                f"{self._base_url}/sendMessage",
                json=payload,
                timeout=self.timeout_seconds,
            )
            if resp.status_code == 400 and parse_mode:
                logger.warning("Erreur HTML Telegram (400), repli en texte brut.")
                payload.pop("parse_mode", None)
                resp = requests.post(
                    f"{self._base_url}/sendMessage",
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            if not resp.ok:
                logger.error(
                    "Échec envoi Telegram (%s): %s", resp.status_code, resp.text
                )
                resp.raise_for_status()

    def verify_bot(self) -> str:
        """Vérifie la validité du token Telegram et retourne le nom du bot."""
        resp = requests.get(
            f"{self._base_url}/getMe", timeout=self.timeout_seconds
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Réponse Telegram invalide: {data}")
        username = data["result"].get("username", "inconnu")
        return str(username)

    def send_test_message(self) -> None:
        """Envoie un message de vérification sur le chat configuré."""
        self._send_raw(
            "✅ <b>Secrétaire IA INSA Rouen connecté !</b>\n"
            "Les alertes urgentes et le récapitulatif du soir arriveront ici."
        )

    def send_startup_message(
        self, settings: Settings, metrics: SystemMetrics | None = None
    ) -> None:
        """Notifie sur Telegram à chaque démarrage ou redémarrage du bot."""
        info = metrics or collect_system_metrics(settings.db_path)
        now_str = datetime.now(settings.timezone).strftime("%d/%m %H:%M:%S")
        lines = [
            "🟢 <b>Secrétaire IA redémarré et actif</b>",
            f"🖥️ <b>Hôte :</b> <code>{html.escape(info.hostname)}</code> ({html.escape(info.os_label)})",
            f"🕒 <b>Heure :</b> {now_str}",
            f"🌙 <b>Récap du soir :</b> {settings.digest_hour:02d}h{settings.digest_minute:02d} (dès J-{settings.digest_lookback_days})",
        ]
        if info.ram_total_mb is not None and info.ram_used_mb is not None:
            lines.append(
                f"🧠 <b>RAM VPS :</b> {info.ram_used_mb:.0f} / {info.ram_total_mb:.0f} Mo "
                f"(libre : {info.ram_available_mb:.0f} Mo)"
            )
        if info.bot_rss_mb is not None:
            lines.append(f"🤖 <b>RAM bot :</b> {info.bot_rss_mb:.1f} Mo")
        self._send_raw("\n".join(lines), disable_notification=False)

    def send_urgent_alert(
        self,
        mail: ParsedEmail,
        classification: EmailClassification,
        *,
        tz: ZoneInfo | None = None,
    ) -> None:
        """Formate et envoie une alerte en direct avec boutons d'action rapide."""
        badge = "🚨 <b>MAIL URGENT</b>" if classification.category == "urgent" else "📩 <b>MAIL IMPORTANT</b>"
        sender = html.escape(mail.sender_display)
        title = html.escape(classification.short_title)
        subject = html.escape(mail.subject)
        summary = html.escape(classification.summary)

        lines = [
            f"{badge}",
            f"<b>{title}</b>",
            f"👤 <b>De :</b> {sender}",
            f"🏷️ <b>Objet :</b> {subject}",
        ]
        if mail.attachment_names:
            att = html.escape(", ".join(mail.attachment_names[:4]))
            lines.append(f"📎 <b>Pièces jointes :</b> {att}")
        lines.extend(["", summary])

        if classification.action_required:
            action = html.escape(classification.action_required)
            lines.append("")
            lines.append(f"👉 <b>Action :</b> {action}")

        keyboard: list[list[dict[str, str]]] = [
            [
                {"text": "📖 Lire", "callback_data": f"read:{mail.uid}"},
                {"text": "✅ Lu", "callback_data": f"seen:{mail.uid}"},
                {"text": "🗑️ Corbeille", "callback_data": f"trash:{mail.uid}"},
            ],
            [
                {"text": "✍️ Répondre", "callback_data": f"reply:{mail.uid}"},
                {"text": "🔕 Silencieux", "callback_data": f"mute:{mail.uid}"},
            ],
        ]

        if classification.calendar_event is not None:
            cal_url = build_google_calendar_url(classification.calendar_event, tz=tz)
            if cal_url:
                ev_title = html.escape(classification.calendar_event.title)
                ev_start = html.escape(classification.calendar_event.start_iso)
                lines.append("")
                lines.append(f"📅 <b>Événement :</b> {ev_title} ({ev_start})")
                keyboard.append([{"text": "📅 Google Agenda", "url": cal_url}])

        self._send_raw(
            "\n".join(lines),
            disable_notification=False,
            reply_markup={"inline_keyboard": keyboard},
        )

    def send_daily_digest(
        self,
        count: int,
        digest_body: str,
        preview: bool = False,
    ) -> None:
        """Envoie le récapitulatif quotidien (ou un aperçu anticipé) sur Telegram."""
        title = "👀 <b>Aperçu du récap INSA" if preview else "🌙 <b>Récap du soir INSA"
        header = (
            f"{title} ({count} mail{'s' if count > 1 else ''} "
            f"secondaire{'s' if count > 1 else ''})</b>\n\n"
        )
        formatted_body = markdown_to_telegram_html(digest_body)
        self._send_raw(header + formatted_body, disable_notification=False)

    def send_plain_reply(self, text: str) -> None:
        """Envoie une réponse conversationnelle formatée proprement pour Telegram."""
        self._send_raw(markdown_to_telegram_html(text), disable_notification=False)

    def send_confirmation_prompt(self, text: str, action_id: str) -> None:
        """Envoie un message avec boutons Confirmer / Annuler."""
        markup: dict[str, object] = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirmer", "callback_data": f"confirm:{action_id}"},
                    {"text": "❌ Annuler", "callback_data": f"cancel:{action_id}"},
                ]
            ]
        }
        self._send_raw(
            html.escape(text),
            disable_notification=False,
            reply_markup=markup,
        )

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> None:
        """Acquitte un clic sur un bouton inline Telegram."""
        try:
            payload: dict[str, object] = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            requests.post(
                f"{self._base_url}/answerCallbackQuery",
                json=payload,
                timeout=5,
            )
        except Exception:
            pass

    def send_chat_action(self, action: str = "typing") -> None:
        """Affiche l'indicateur 'en train d'écrire...' dans Telegram."""
        try:
            requests.post(
                f"{self._base_url}/sendChatAction",
                json={"chat_id": self.chat_id, "action": action},
                timeout=5,
            )
        except Exception:
            pass

    def get_updates(
        self, offset: int | None = None, timeout: int = 25
    ) -> list[dict[str, object]]:
        """Récupère les nouveaux messages et clics boutons via long-polling Telegram."""
        params: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset

        resp = requests.get(
            f"{self._base_url}/getUpdates",
            params=params,
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return []
        result = data.get("result")
        return result if isinstance(result, list) else []
