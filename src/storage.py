"""Persistance SQLite pour la déduplication des mails et la file du récap du soir."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class StoredNewsletter:
    """Représente un mail mis de côté pour le récapitulatif du soir."""

    id: int
    sender: str
    subject: str
    short_title: str
    summary: str
    received_at: str


@dataclass(frozen=True)
class StoredImportantEmail:
    """Mail important récent suivi pour détecter l'absence de réponse."""

    uid: int
    message_id: str | None
    sender_email: str
    sender: str
    subject: str
    received_at: str
    action_required: str | None


class StateStore:
    """Stockage local SQLite gérant l'état du secrétaire IA."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    message_id TEXT,
                    sender TEXT NOT NULL,
                    sender_email TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    short_title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    action_required TEXT,
                    current_folder TEXT NOT NULL DEFAULT 'INBOX',
                    is_direct_or_action INTEGER NOT NULL DEFAULT 0,
                    notified_now INTEGER NOT NULL DEFAULT 0,
                    digested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(uidvalidity, uid)
                )
                """
            )
            existing_cols = {
                str(r["name"])
                for r in self._conn.execute("PRAGMA table_info(processed_emails)").fetchall()
            }
            for col_name, col_def in (
                ("sender_email", "TEXT NOT NULL DEFAULT ''"),
                ("current_folder", "TEXT NOT NULL DEFAULT 'INBOX'"),
                ("is_direct_or_action", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col_name not in existing_cols:
                    self._conn.execute(
                        f"ALTER TABLE processed_emails ADD COLUMN {col_name} {col_def}"
                    )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_message_id
                ON processed_emails(message_id)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS muted_senders (
                    pattern TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def is_already_processed(
        self, uidvalidity: int, uid: int, message_id: str | None
    ) -> bool:
        """Vérifie si un mail a déjà été traité (par UID ou Message-ID)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_emails WHERE uidvalidity = ? AND uid = ? LIMIT 1",
                (uidvalidity, uid),
            ).fetchone()
            if row is not None:
                return True

            if message_id:
                row = self._conn.execute(
                    "SELECT 1 FROM processed_emails WHERE message_id = ? LIMIT 1",
                    (message_id,),
                ).fetchone()
                if row is not None:
                    return True
            return False

    def record_email(
        self,
        *,
        uidvalidity: int,
        uid: int,
        message_id: str | None,
        sender: str,
        sender_email: str = "",
        subject: str,
        received_at: str,
        category: str,
        short_title: str,
        summary: str,
        action_required: str | None,
        current_folder: str = "INBOX",
        is_direct_or_action: bool = False,
        notified_now: bool,
        digested: bool,
    ) -> int:
        """Enregistre un mail traité et retourne son ID interne."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO processed_emails (
                    uidvalidity, uid, message_id, sender, sender_email, subject, received_at,
                    category, short_title, summary, action_required,
                    current_folder, is_direct_or_action,
                    notified_now, digested, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uidvalidity,
                    uid,
                    message_id,
                    sender,
                    sender_email.lower(),
                    subject,
                    received_at,
                    category,
                    short_title,
                    summary,
                    action_required,
                    current_folder,
                    int(is_direct_or_action),
                    int(notified_now),
                    int(digested),
                    now_iso,
                ),
            )
            return int(cursor.lastrowid or 0)

    def get_undigested_newsletters(
        self, include_since_iso: str | None = None
    ) -> list[StoredNewsletter]:
        """Retourne les mails secondaires en attente (ou ceux de la fenêtre pour /recap)."""
        with self._lock:
            if include_since_iso:
                rows = self._conn.execute(
                    """
                    SELECT id, sender, subject, short_title, summary, received_at
                    FROM processed_emails
                    WHERE (digested = 0 OR received_at >= ?)
                      AND notified_now = 0
                      AND category != 'archived'
                    ORDER BY received_at ASC, id ASC
                    """,
                    (include_since_iso,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT id, sender, subject, short_title, summary, received_at
                    FROM processed_emails
                    WHERE digested = 0
                      AND notified_now = 0
                      AND category != 'archived'
                    ORDER BY received_at ASC, id ASC
                    """
                ).fetchall()
        return [
            StoredNewsletter(
                id=int(row["id"]),
                sender=str(row["sender"]),
                subject=str(row["subject"]),
                short_title=str(row["short_title"]),
                summary=str(row["summary"]),
                received_at=str(row["received_at"]),
            )
            for row in rows
        ]

    def mark_digested(self, email_ids: list[int]) -> None:
        """Marque une liste de mails comme inclus dans le récap du soir."""
        if not email_ids:
            return
        placeholders = ",".join("?" for _ in email_ids)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE processed_emails SET digested = 1 WHERE id IN ({placeholders})",
                email_ids,
            )

    def get_email_by_uid(self, uid: int) -> dict[str, object] | None:
        """Retourne les métadonnées enregistrées pour un UID donné."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT uid, uidvalidity, message_id, sender, sender_email, subject, current_folder
                FROM processed_emails
                WHERE uid = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_email_folder(self, uid: int, folder: str) -> None:
        """Met à jour le dossier connu d'un mail après déplacement."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE processed_emails SET current_folder = ? WHERE uid = ?",
                (folder, uid),
            )

    def add_muted_sender(self, pattern: str) -> None:
        """Ajoute un expéditeur à la liste silencieuse apprise en base."""
        clean = pattern.strip().lower()
        if not clean:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO muted_senders (pattern, created_at)
                VALUES (?, ?)
                """,
                (clean, now_iso),
            )

    def get_muted_senders(self) -> tuple[str, ...]:
        """Retourne les expéditeurs mis en silencieux depuis Telegram."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pattern FROM muted_senders ORDER BY pattern ASC"
            ).fetchall()
        return tuple(str(r["pattern"]) for r in rows)

    def get_recent_important_candidates(self, days: int = 5) -> list[StoredImportantEmail]:
        """Retourne les mails importants récents pouvant attendre une réponse."""
        if days <= 0:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT uid, message_id, sender_email, sender, subject, received_at, action_required
                FROM processed_emails
                WHERE notified_now = 1
                  AND is_direct_or_action = 1
                  AND created_at >= ?
                ORDER BY id DESC
                LIMIT 30
                """,
                (cutoff,),
            ).fetchall()
        return [
            StoredImportantEmail(
                uid=int(row["uid"]),
                message_id=str(row["message_id"]) if row["message_id"] else None,
                sender_email=str(row["sender_email"] or ""),
                sender=str(row["sender"]),
                subject=str(row["subject"]),
                received_at=str(row["received_at"]),
                action_required=str(row["action_required"])
                if row["action_required"]
                else None,
            )
            for row in rows
        ]

    def _get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ? LIMIT 1", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def _set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_last_uid(self, uidvalidity: int) -> int | None:
        raw = self._get_meta(f"last_uid:{uidvalidity}")
        return int(raw) if raw is not None else None

    def set_last_uid(self, uidvalidity: int, uid: int) -> None:
        self._set_meta(f"last_uid:{uidvalidity}", str(uid))

    def get_last_digest_date(self) -> str | None:
        return self._get_meta("last_digest_date")

    def set_last_digest_date(self, date_str: str) -> None:
        self._set_meta("last_digest_date", date_str)

    def get_last_telegram_update_id(self) -> int | None:
        raw = self._get_meta("last_telegram_update_id")
        return int(raw) if raw is not None else None

    def set_last_telegram_update_id(self, update_id: int) -> None:
        self._set_meta("last_telegram_update_id", str(update_id))

    def get_stats(self) -> dict[str, int]:
        """Retourne le nombre total de mails traités et ceux en attente de récap."""
        with self._lock:
            total = int(
                self._conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]
            )
            pending = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM processed_emails WHERE digested = 0 AND notified_now = 0"
                ).fetchone()[0]
            )
        return {"total_processed": total, "pending_digest": pending}
