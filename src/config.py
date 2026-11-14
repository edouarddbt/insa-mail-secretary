"""Chargement et validation de la configuration depuis les variables d'environnement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _parse_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _parse_bool(raw: str, default: bool = False) -> bool:
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "oui", "on"}


def _parse_digest_time(raw: str) -> tuple[int, int]:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"DIGEST_TIME invalide ({raw!r}), format attendu HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"DIGEST_TIME hors plage ({raw!r})")
    return hour, minute


@dataclass(frozen=True)
class Settings:
    """Paramètres complets de l'application."""

    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_folder: str

    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]

    telegram_bot_token: str
    telegram_chat_id: str

    timezone: ZoneInfo
    digest_hour: int
    digest_minute: int
    digest_send_when_empty: bool
    idle_timeout_seconds: int
    poll_fallback_seconds: int
    initial_lookback_hours: int
    max_email_age_hours: int
    auto_archive_newsletters: bool
    archive_folder: str
    reply_reminder_days: int

    user_context: str
    vip_senders: tuple[str, ...]
    muted_senders: tuple[str, ...]
    db_path: Path
    digest_lookback_days: int = 1

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Settings":
        """Charge les paramètres depuis `.env` et les variables d'environnement."""
        load_dotenv(dotenv_path=env_file)

        digest_hour, digest_minute = _parse_digest_time(
            os.getenv("DIGEST_TIME", "20:00")
        )
        tz_name = os.getenv("TIMEZONE", "Europe/Paris").strip()

        return cls(
            imap_host=os.getenv("IMAP_HOST", "partage.insa-rouen.fr").strip(),
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            imap_user=os.getenv("IMAP_USER", "").strip(),
            imap_password=os.getenv("IMAP_PASSWORD", ""),
            imap_folder=os.getenv("IMAP_FOLDER", "INBOX").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip(),
            gemini_fallback_models=_parse_csv(
                os.getenv(
                    "GEMINI_FALLBACK_MODELS",
                    "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.7-flash",
                )
            ),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            timezone=ZoneInfo(tz_name),
            digest_hour=digest_hour,
            digest_minute=digest_minute,
            digest_send_when_empty=_parse_bool(
                os.getenv("DIGEST_SEND_WHEN_EMPTY", "false")
            ),
            idle_timeout_seconds=int(os.getenv("IDLE_TIMEOUT_SECONDS", "600")),
            poll_fallback_seconds=int(os.getenv("POLL_FALLBACK_SECONDS", "300")),
            initial_lookback_hours=int(os.getenv("INITIAL_LOOKBACK_HOURS", "0")),
            max_email_age_hours=int(os.getenv("MAX_EMAIL_AGE_HOURS", "12")),
            auto_archive_newsletters=_parse_bool(
                os.getenv("AUTO_ARCHIVE_NEWSLETTERS", "false"), default=False
            ),
            archive_folder=os.getenv("ARCHIVE_FOLDER", "").strip(),
            reply_reminder_days=int(os.getenv("REPLY_REMINDER_DAYS", "5")),
            user_context=os.getenv(
                "USER_CONTEXT",
                "Étudiant en 4ème année au département ITI (ITI4) à l'INSA Rouen Normandie.",
            ).strip(),
            vip_senders=_parse_csv(os.getenv("VIP_SENDERS", "")),
            muted_senders=_parse_csv(os.getenv("MUTED_SENDERS", "")),
            db_path=Path(os.getenv("DB_PATH", "data/secretary.db")),
            digest_lookback_days=int(os.getenv("DIGEST_LOOKBACK_DAYS", "1")),
        )

    def validate_imap(self) -> None:
        if not self.imap_user or not self.imap_password:
            raise ValueError("IMAP_USER et IMAP_PASSWORD doivent être renseignés.")

    def validate_gemini(self) -> None:
        if not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY doit être renseignée.")

    def validate_telegram(self) -> None:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être renseignés."
            )

    def validate_all(self) -> None:
        self.validate_imap()
        self.validate_gemini()
        self.validate_telegram()
