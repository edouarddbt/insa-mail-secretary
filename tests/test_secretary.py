"""Tests unitaires du secrétaire IA (sans appel réseau réel)."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.bot_assistant import MailSearchService, PendingMoveAction, TelegramAssistantLoop
from src.classifier import CalendarEvent, EmailClassification, MailClassifier
from src.config import Settings
from src.email_parser import html_to_text, parse_raw_email
from src.imap_tools import ImapMailboxTools, to_ascii_term
from src.imap_watcher import ImapSecretary
from src.notifier import (
    TelegramNotifier,
    build_google_calendar_url,
    markdown_to_telegram_html,
    split_message,
)
from src.storage import StateStore
from src.system_info import collect_system_metrics, format_vps_status


def _make_settings(db_path: Path) -> Settings:
    return Settings(
        imap_host="partage.insa-rouen.fr",
        imap_port=993,
        imap_user="etudiant@insa-rouen.fr",
        imap_password="secret",
        imap_folder="INBOX",
        gemini_api_key="fake-key",
        gemini_model="gemini-3.5-flash-lite",
        gemini_fallback_models=("gemini-3.1-flash-lite",),
        telegram_bot_token="123:abc",
        telegram_chat_id="999",
        timezone=ZoneInfo("Europe/Paris"),
        digest_hour=20,
        digest_minute=0,
        digest_send_when_empty=False,
        idle_timeout_seconds=600,
        poll_fallback_seconds=300,
        initial_lookback_hours=0,
        max_email_age_hours=12,
        user_context="Étudiant INSA Rouen",
        vip_senders=("scolarite@insa-rouen.fr",),
        muted_senders=("bde@insa-rouen.fr",),
        db_path=db_path,
        auto_archive_newsletters=True,
        archive_folder="",
        reply_reminder_days=5,
    )


class EmailParserTests(unittest.TestCase):
    def test_html_to_text_strips_scripts_and_tags(self) -> None:
        raw_html = (
            "<html><head><style>.x{color:red;}</style></head>"
            "<body><script>alert(1)</script>"
            "<h1>Info Campus</h1><p>Bonjour <b>à tous</b>!</p>"
            "</body></html>"
        )
        text = html_to_text(raw_html)
        self.assertIn("Info Campus", text)
        self.assertIn("Bonjour à tous!", text)
        self.assertNotIn("alert(1)", text)
        self.assertNotIn("color:red", text)

    def test_parse_raw_email_detects_bulk_list_and_body(self) -> None:
        msg = EmailMessage()
        msg["From"] = "BDE INSA <bde@insa-rouen.fr>"
        msg["To"] = "etudiant@insa-rouen.fr"
        msg["Subject"] = "Soirée jeudi !"
        msg["Message-ID"] = "<abc123@insa-rouen.fr>"
        msg["List-Unsubscribe"] = "<mailto:unsub@insa-rouen.fr>"
        msg.set_content("Venez nombreux jeudi soir au foyer.")

        parsed = parse_raw_email(42, msg.as_bytes())
        self.assertEqual(parsed.uid, 42)
        self.assertEqual(parsed.sender_email, "bde@insa-rouen.fr")
        self.assertEqual(parsed.subject, "Soirée jeudi !")
        self.assertTrue(parsed.is_bulk_list)
        self.assertIn("jeudi soir", parsed.body_text)


class NotifierTests(unittest.TestCase):
    def test_markdown_to_telegram_html_converts_bold_italic_bullets(self) -> None:
        raw = (
            "1. **Modalités** — *De Prof*\n"
            "* **Sujet :** Note <CC> & examen\n"
            "  * Détail en *italique*"
        )
        converted = markdown_to_telegram_html(raw)
        self.assertIn("<b>Modalités</b>", converted)
        self.assertIn("<i>De Prof</i>", converted)
        self.assertIn("• <b>Sujet :</b> Note &lt;CC&gt; &amp; examen", converted)
        self.assertIn("  • Détail en <i>italique</i>", converted)
        self.assertIn("\n\n  • Détail", converted)
        self.assertNotIn("**", converted)

    def test_split_message_respects_max_length(self) -> None:
        long_text = "\n".join(f"Ligne {i} " + ("x" * 40) for i in range(20))
        chunks = split_message(long_text, max_len=200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200)


class StorageAndSecretaryTests(unittest.TestCase):
    def test_dedup_and_daily_digest_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                classifier = MagicMock()
                notifier = MagicMock()
                mailbox = MagicMock()
                mailbox.find_folder_by_role.return_value = None
                mailbox.fetch_recent_emails_since.return_value = (100, [])
                secretary = ImapSecretary(
                    settings, store, classifier, notifier, mailbox=mailbox
                )

                # Simule un mail newsletter mis en file pour le soir
                store.record_email(
                    uidvalidity=100,
                    uid=10,
                    message_id="<nl1@insa-rouen.fr>",
                    sender="bde@insa-rouen.fr",
                    subject="Tournoi foot",
                    received_at="2026-11-12T14:00:00+01:00",
                    category="newsletter",
                    short_title="Tournoi de foot BDE",
                    summary="Inscriptions ouvertes jusqu'à vendredi.",
                    action_required=None,
                    notified_now=False,
                    digested=False,
                )
                self.assertTrue(
                    store.is_already_processed(100, 10, "<nl1@insa-rouen.fr>")
                )

                classifier.summarize_digest.return_value = "• Tournoi de foot BDE"

                # Avant 20h00 -> pas d'envoi automatique
                before_target = datetime(
                    2026, 11, 12, 19, 30, tzinfo=settings.timezone
                )
                with patch("src.imap_watcher.datetime") as mock_dt:
                    mock_dt.now.return_value = before_target
                    sent = secretary.maybe_send_daily_digest(force=False)
                self.assertFalse(sent)
                notifier.send_daily_digest.assert_not_called()

                # À 20h05 -> envoi du récap
                after_target = datetime(
                    2026, 11, 12, 20, 5, tzinfo=settings.timezone
                )
                with patch("src.imap_watcher.datetime") as mock_dt:
                    mock_dt.now.return_value = after_target
                    sent = secretary.maybe_send_daily_digest(force=False)
                    self.assertTrue(sent)
                    # Un second appel le même jour ne renvoie rien
                    sent_again = secretary.maybe_send_daily_digest(force=False)
                    self.assertFalse(sent_again)

                notifier.send_daily_digest.assert_called_once_with(
                    1, "• Tournoi de foot BDE"
                )
                self.assertEqual(store.get_undigested_newsletters(), [])
            finally:
                store.close()

    def test_first_run_initializes_last_uid_without_replaying_old_mails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                secretary = ImapSecretary(
                    settings, store, MagicMock(), MagicMock()
                )
                fake_imap = MagicMock()
                fake_imap.search.return_value = [1, 2, 50]
                candidates = secretary._fetch_candidate_uids(fake_imap, 777)
                self.assertEqual(candidates, [])
                self.assertEqual(store.get_last_uid(777), 50)
            finally:
                store.close()

    def test_process_new_messages_uses_body_peek_and_notifies_urgent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            store.set_last_uid(777, 50)
            try:
                classifier = MagicMock()
                notifier = MagicMock()
                classifier.classify.return_value = EmailClassification(
                    category="urgent",
                    notify_now=True,
                    short_title="Changement de salle",
                    summary="Examen déplacé en Amphi D.",
                    action_required="Aller en Amphi D à 8h30",
                )

                msg = EmailMessage()
                msg["From"] = "Professeur <prof@insa-rouen.fr>"
                msg["To"] = "etudiant@insa-rouen.fr"
                msg["Subject"] = "Changement salle"
                msg["Date"] = format_datetime(datetime.now(timezone.utc))
                msg["Message-ID"] = "<urgent1@insa-rouen.fr>"
                msg.set_content("Examen en Amphi D.")

                fake_imap = MagicMock()
                fake_imap.search.return_value = [50, 51]
                fake_imap.fetch.return_value = {51: {b"BODY[]": msg.as_bytes()}}

                secretary = ImapSecretary(settings, store, classifier, notifier)
                count = secretary.process_new_messages(fake_imap, 777)

                self.assertEqual(count, 1)
                fake_imap.fetch.assert_called_once_with([51], ["BODY.PEEK[]"])
                notifier.send_urgent_alert.assert_called_once()
                self.assertEqual(store.get_last_uid(777), 51)
                self.assertEqual(store.get_undigested_newsletters(), [])
            finally:
                store.close()

    def test_skips_old_restored_emails_without_notifying_or_calling_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            store.set_last_uid(777, 50)
            try:
                classifier = MagicMock()
                notifier = MagicMock()

                msg = EmailMessage()
                msg["From"] = "Scolarité <scol@insa-rouen.fr>"
                msg["To"] = "etudiant@insa-rouen.fr"
                msg["Subject"] = "Ancien mail restauré"
                msg["Date"] = "Thu, 01 Oct 2026 10:00:00 +0200"
                msg["Message-ID"] = "<old1@insa-rouen.fr>"
                msg.set_content("Vieux mail déplacé depuis la corbeille.")

                fake_imap = MagicMock()
                fake_imap.search.return_value = [50, 52]
                fake_imap.fetch.return_value = {52: {b"BODY[]": msg.as_bytes()}}

                secretary = ImapSecretary(settings, store, classifier, notifier)
                count = secretary.process_new_messages(fake_imap, 777)

                self.assertEqual(count, 0)
                classifier.classify.assert_not_called()
                notifier.send_urgent_alert.assert_not_called()
                self.assertEqual(store.get_last_uid(777), 52)
                self.assertEqual(store.get_undigested_newsletters(), [])
            finally:
                store.close()


class ClassifierFallbackTests(unittest.TestCase):
    def test_falls_back_to_secondary_model_when_primary_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            with patch("src.classifier.genai.Client") as mock_client_cls:
                mock_client = mock_client_cls.return_value
                expected = EmailClassification(
                    category="important",
                    notify_now=True,
                    short_title="Test",
                    summary="Résumé",
                    action_required=None,
                )
                ok_resp = MagicMock()
                ok_resp.parsed = expected
                mock_client.models.generate_content.side_effect = [
                    RuntimeError("503 UNAVAILABLE"),
                    ok_resp,
                ]

                classifier = MailClassifier(settings)
                mail = parse_raw_email(
                    1,
                    b"From: prof@insa-rouen.fr\r\nSubject: Cours\r\n\r\nBonjour",
                )
                res = classifier.classify(mail, raise_on_error=True)

                self.assertEqual(res.short_title, "Test")
                calls = mock_client.models.generate_content.call_args_list
                self.assertEqual(calls[0].kwargs["model"], "gemini-3.5-flash-lite")
                self.assertEqual(calls[1].kwargs["model"], "gemini-3.1-flash-lite")


class TelegramAssistantTests(unittest.TestCase):
    def test_to_ascii_term_removes_accents(self) -> None:
        self.assertEqual(to_ascii_term("  Réunion égalité  "), "Reunion egalite")

    def test_ignores_unauthorized_chat_and_answers_authorized_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                notifier = MagicMock()
                search_service = MagicMock()
                search_service.run_agent.return_value = ("Réunion jeudi à 14h.", None)
                loop = TelegramAssistantLoop(
                    settings, store, notifier, search_service
                )

                # Chat inconnu -> ignoré
                loop.handle_update(
                    {"message": {"chat": {"id": 111}, "text": "Donne mes mails"}}
                )
                search_service.run_agent.assert_not_called()
                notifier.send_plain_reply.assert_not_called()

                # Chat autorisé (999) -> répond
                loop.handle_update(
                    {
                        "message": {
                            "chat": {"id": 999},
                            "text": "C'est quand la réunion ?",
                        }
                    }
                )
                search_service.run_agent.assert_called_once_with(
                    "C'est quand la réunion ?", conversation_history=[]
                )
                notifier.send_chat_action.assert_called_once_with("typing")
                notifier.send_plain_reply.assert_called_once_with(
                    "Réunion jeudi à 14h."
                )
            finally:
                store.close()

    def test_confirmation_button_executes_move_and_rejects_unknown_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                notifier = MagicMock()
                search_service = MagicMock()
                pending = PendingMoveAction(
                    action_id="act123",
                    source_folder="Trash",
                    target_folder="INBOX",
                    uids=[10, 11],
                    preview_lines=["[UID 10] Prof", "[UID 11] Scolarité"],
                    reason="Restaurer mails importants",
                    created_at=1e12,
                )
                search_service.run_agent.return_value = (
                    "J'ai trouvé 2 mails à restaurer.",
                    pending,
                )
                search_service.mailbox.move_emails.return_value = 2

                loop = TelegramAssistantLoop(
                    settings, store, notifier, search_service
                )
                loop.handle_update(
                    {"message": {"chat": {"id": 999}, "text": "Restaure mes mails"}}
                )
                notifier.send_confirmation_prompt.assert_called_once()

                # Callback d'un autre chat -> ignoré
                loop.handle_update(
                    {
                        "callback_query": {
                            "id": "cb1",
                            "data": "confirm:act123",
                            "message": {"chat": {"id": 111}},
                        }
                    }
                )
                search_service.mailbox.move_emails.assert_not_called()

                # Callback autorisé -> exécute le déplacement
                loop.handle_update(
                    {
                        "callback_query": {
                            "id": "cb2",
                            "data": "confirm:act123",
                            "message": {"chat": {"id": 999}},
                        }
                    }
                )
                search_service.mailbox.move_emails.assert_called_once_with(
                    "Trash", "INBOX", [10, 11]
                )
            finally:
                store.close()

    def test_imap_mailbox_tools_list_search_and_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            tools = ImapMailboxTools(settings)

            msg = EmailMessage()
            msg["From"] = "Secrétariat <sec@insa-rouen.fr>"
            msg["Subject"] = "Réunion stage"
            msg.set_content("La réunion stage a lieu mardi en B102.")

            with patch("src.imap_tools.IMAPClient") as mock_imap_cls:
                mock_imap = mock_imap_cls.return_value
                mock_imap.list_folders.return_value = [
                    ((b"\\HasNoChildren",), b"/", b"INBOX"),
                    ((b"\\Trash",), b"/", b"Corbeille"),
                    ((b"\\Junk",), b"/", b"Spam"),
                ]
                folders = tools.list_folders()
                self.assertEqual(
                    [(f.name, f.role) for f in folders],
                    [("INBOX", "inbox"), ("Corbeille", "trash"), ("Spam", "junk")],
                )

                mock_imap.search.return_value = [12]
                mock_imap.fetch.return_value = {12: {b"BODY[]<0>": msg.as_bytes()}}
                found = tools.search_folder(folder="Corbeille", keywords=["réunion"])
                mock_imap.select_folder.assert_called_with("Corbeille", readonly=True)
                mock_imap.search.assert_called_with(["TEXT", "reunion"])
                mock_imap.fetch.assert_called_once_with([12], ["BODY.PEEK[]<0.65536>"])
                self.assertEqual(len(found), 1)

                mock_imap.has_capability.return_value = True
                moved = tools.move_emails("Corbeille", "INBOX", [12])
                self.assertEqual(moved, 1)
                mock_imap.select_folder.assert_called_with("Corbeille", readonly=False)
                mock_imap.move.assert_called_once_with([12], "INBOX")

    def test_agent_tool_calling_builds_schemas_and_proposes_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            classifier = MailClassifier(settings)
            mailbox = MagicMock()
            msg = parse_raw_email(
                77,
                b"From: Scolarite <scol@insa-rouen.fr>\r\nSubject: Note\r\n\r\nUrgent",
            )
            mailbox.read_emails_by_uids.return_value = [msg]

            call_1 = MagicMock()
            call_1.name = "propose_move_emails"
            call_1.args = {
                "source_folder": "Trash",
                "target_folder": "INBOX",
                "uids": [77],
                "reason": "Mail de la scolarité",
            }
            resp_1 = MagicMock()
            resp_1.candidates = [MagicMock(content=MagicMock())]
            resp_1.function_calls = [call_1]

            resp_2 = MagicMock()
            resp_2.candidates = [MagicMock(content=MagicMock())]
            resp_2.function_calls = []
            resp_2.text = "Voici le mail trouvé dans ta corbeille."

            service = MailSearchService(settings, classifier, mailbox=mailbox)
            captured_histories: list[list[object]] = []

            def fake_generate(contents: object, config: object) -> object:
                assert isinstance(contents, list)
                captured_histories.append(list(contents))
                return resp_1 if len(captured_histories) == 1 else resp_2

            with patch.object(
                classifier, "generate_with_fallback", side_effect=fake_generate
            ):
                reply, pending = service.run_agent(
                    "Restaure le mail 77",
                    conversation_history=[("Bonjour", "Salut !")],
                )

            self.assertEqual(reply, "Voici le mail trouvé dans ta corbeille.")
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending.uids, [77])
            self.assertEqual(pending.source_folder, "Trash")
            self.assertEqual(pending.target_folder, "INBOX")
            # Vérifie que la réponse d'outil utilise bien role='user' (exigé par Gemini)
            self.assertEqual(captured_histories[1][-1].role, "user")
            self.assertEqual(captured_histories[0][0].role, "user")
            self.assertEqual(captured_histories[0][1].role, "model")

    def test_search_folder_supports_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            tools = ImapMailboxTools(settings)
            with patch("src.imap_tools.IMAPClient") as mock_imap_cls:
                mock_imap = mock_imap_cls.return_value
                mock_imap.search.return_value = []
                tools.search_folder(
                    folder="Trash", date_from="2026-11-05", date_to="2026-11-07"
                )
                from datetime import date

                mock_imap.search.assert_called_once_with(
                    ["SENTSINCE", date(2026, 11, 5), "SENTBEFORE", date(2026, 11, 8)]
                )

    def test_search_folder_sorts_by_envelope_date_even_if_older_mail_has_higher_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            tools = ImapMailboxTools(settings)

            newer_msg = EmailMessage()
            newer_msg["From"] = "outgoingstudents@insa-rouen.fr"
            newer_msg["Subject"] = "Presentation 12 nov"
            newer_msg["Date"] = "Thu, 12 Nov 2026 11:37:00 +0100"
            newer_msg.set_content("Aujourd'hui 11h37")

            older_restored_msg = EmailMessage()
            older_restored_msg["From"] = "Walid MATAICH <walid@insa-rouen.fr>"
            older_restored_msg["Subject"] = "Laser Run 9 nov"
            older_restored_msg["Date"] = "Mon, 09 Nov 2026 21:53:00 +0100"
            older_restored_msg.set_content("Mail du 9 nov restauré avec un UID plus haut")

            env_newer = MagicMock(date=datetime(2026, 11, 12, 11, 37, tzinfo=settings.timezone))
            env_older = MagicMock(date=datetime(2026, 11, 9, 21, 53, tzinfo=settings.timezone))

            with patch("src.imap_tools.IMAPClient") as mock_imap_cls:
                mock_imap = mock_imap_cls.return_value
                mock_imap.search.return_value = [5232, 5270]
                mock_imap.fetch.side_effect = [
                    {5232: {b"ENVELOPE": env_newer}, 5270: {b"ENVELOPE": env_older}},
                    {5232: {b"BODY[]": newer_msg.as_bytes()}},
                ]

                found = tools.search_folder(folder="INBOX", max_results=1)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].uid, 5232)
                self.assertEqual(found[0].subject, "Presentation 12 nov")


class SixNewFeaturesTests(unittest.TestCase):
    def test_build_google_calendar_url_and_urgent_alert_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            notifier = TelegramNotifier(settings)
            event = CalendarEvent(
                title="Soutenance PFE",
                start_iso="2026-11-20T14:00:00",
                end_iso="2026-11-20T15:30:00",
                location="Amphi A INSA",
                details="Apporter le rapport.",
            )
            url = build_google_calendar_url(event, tz=settings.timezone)
            self.assertIsNotNone(url)
            assert url is not None
            self.assertIn("calendar.google.com/calendar/render", url)
            self.assertIn("20261120T130000Z%2F20261120T143000Z", url)

            mail = parse_raw_email(
                88,
                b"From: prof@insa-rouen.fr\r\nSubject: Soutenance\r\n\r\nRappel soutenance.",
            )
            classification = EmailClassification(
                category="urgent",
                notify_now=True,
                short_title="Soutenance PFE",
                summary="Votre soutenance a lieu le 20 nov à 14h.",
                action_required="Préparer les slides",
                calendar_event=event,
            )
            with patch("src.notifier.requests.post") as mock_post:
                mock_post.return_value = MagicMock(ok=True, status_code=200)
                notifier.send_urgent_alert(mail, classification, tz=settings.timezone)

            payload = mock_post.call_args.kwargs["json"]
            keyboard = payload["reply_markup"]["inline_keyboard"]
            flat_callbacks = [
                btn.get("callback_data")
                for row in keyboard
                for btn in row
                if "callback_data" in btn
            ]
            self.assertEqual(
                flat_callbacks,
                ["read:88", "seen:88", "trash:88", "reply:88", "mute:88"],
            )
            url_buttons = [btn for row in keyboard for btn in row if "url" in btn]
            self.assertEqual(len(url_buttons), 1)
            self.assertIn("calendar.google.com", url_buttons[0]["url"])

    def test_parse_raw_email_extracts_pdf_attachment_text(self) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        pdf_buf = io.BytesIO()
        writer.write(pdf_buf)

        msg = EmailMessage()
        msg["From"] = "scolarite@insa-rouen.fr"
        msg["To"] = "etudiant@insa-rouen.fr"
        msg["Subject"] = "Convocation examen"
        msg["In-Reply-To"] = "<prev@insa-rouen.fr>"
        msg.set_content("Voir le PDF joint.")
        msg.add_attachment(
            pdf_buf.getvalue(),
            maintype="application",
            subtype="pdf",
            filename="convocation.pdf",
        )

        with patch("pypdf.PdfReader") as mock_reader_cls:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = "Salle Amphi B le 18 novembre"
            mock_reader_cls.return_value.pages = [mock_page]
            parsed = parse_raw_email(99, msg.as_bytes())

        self.assertEqual(parsed.attachment_names, ("convocation.pdf",))
        self.assertEqual(parsed.in_reply_to, "<prev@insa-rouen.fr>")
        self.assertIn("Pièce jointe PDF : convocation.pdf", parsed.body_text)
        self.assertIn("Salle Amphi B le 18 novembre", parsed.body_text)

    def test_quick_action_callbacks_mute_read_seen_trash_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                store.record_email(
                    uidvalidity=10,
                    uid=55,
                    message_id="<m55@insa-rouen.fr>",
                    sender="Club Robot <robot@insa-rouen.fr>",
                    sender_email="robot@insa-rouen.fr",
                    subject="Réunion club",
                    received_at="2026-11-12T10:00:00+01:00",
                    category="important",
                    short_title="Réunion",
                    summary="Réunion jeudi",
                    action_required=None,
                    current_folder="INBOX",
                    is_direct_or_action=True,
                    notified_now=True,
                    digested=True,
                )
                sample_mail = parse_raw_email(
                    55,
                    b"From: Club Robot <robot@insa-rouen.fr>\r\nSubject: R\xc3\xa9union club\r\n\r\nCorps complet du mail.",
                )
                notifier = MagicMock()
                search_service = MagicMock()
                search_service.mailbox.read_emails_by_uids.return_value = [sample_mail]
                search_service.mailbox.find_folder_by_role.return_value = "Trash"
                search_service.classifier.draft_reply.return_value = "Bonjour,\nMerci !"

                loop = TelegramAssistantLoop(settings, store, notifier, search_service)

                # 1) Mute
                loop.handle_update(
                    {
                        "callback_query": {
                            "id": "c1",
                            "data": "mute:55",
                            "message": {"chat": {"id": 999}},
                        }
                    }
                )
                self.assertIn("robot@insa-rouen.fr", store.get_muted_senders())

                # 2) Read
                loop.handle_update(
                    {
                        "callback_query": {
                            "id": "c2",
                            "data": "read:55",
                            "message": {"chat": {"id": 999}},
                        }
                    }
                )
                self.assertIn("Corps complet du mail.", notifier.send_plain_reply.call_args.args[0])

                # 3) Seen
                loop.handle_update(
                    {
                        "callback_query": {
                            "id": "c3",
                            "data": "seen:55",
                            "message": {"chat": {"id": 999}},
                        }
                    }
                )
                search_service.mailbox.mark_seen.assert_called_once_with(
                    "INBOX", [55], seen=True
                )

                # 4) Reply draft
                loop.handle_update(
                    {
                        "callback_query": {
                            "id": "c4",
                            "data": "reply:55",
                            "message": {"chat": {"id": 999}},
                        }
                    }
                )
                search_service.classifier.draft_reply.assert_called_once_with(sample_mail)

                # 5) Trash
                loop.handle_update(
                    {
                        "callback_query": {
                            "id": "c5",
                            "data": "trash:55",
                            "message": {"chat": {"id": 999}},
                        }
                    }
                )
                search_service.mailbox.move_emails.assert_called_once_with(
                    "INBOX", "Trash", [55]
                )
                self.assertEqual(store.get_email_by_uid(55)["current_folder"], "Trash")
            finally:
                store.close()

    def test_auto_archive_newsletter_and_muted_sender_zero_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            store.set_last_uid(10, 100)
            store.add_muted_sender("club@insa-rouen.fr")
            try:
                with patch("src.classifier.genai.Client") as mock_genai:
                    classifier = MailClassifier(settings)
                    notifier = MagicMock()
                    mailbox = MagicMock()
                    mailbox.ensure_archive_folder.return_value = "Archive"

                    msg = EmailMessage()
                    msg["From"] = "Club <club@insa-rouen.fr>"
                    msg["To"] = "etudiant@insa-rouen.fr"
                    msg["Subject"] = "Info hebdo"
                    msg["Date"] = format_datetime(datetime.now(timezone.utc))
                    msg["Message-ID"] = "<club1@insa-rouen.fr>"
                    msg.set_content("Nouvelles du club.")

                    fake_imap = MagicMock()
                    fake_imap.search.return_value = [100, 101]
                    fake_imap.fetch.return_value = {101: {b"BODY[]": msg.as_bytes()}}

                    secretary = ImapSecretary(
                        settings, store, classifier, notifier, mailbox=mailbox
                    )
                    processed = secretary.process_new_messages(fake_imap, 10)

                    self.assertEqual(processed, 1)
                    # Aucun appel Gemini grâce au mute appris en DB !
                    mock_genai.return_value.models.generate_content.assert_not_called()
                    # Archivage automatique vers Archive
                    mailbox.move_emails.assert_called_once_with("INBOX", "Archive", [101])
                    self.assertEqual(store.get_email_by_uid(101)["current_folder"], "Archive")
            finally:
                store.close()

    def test_pending_reply_reminder_and_db_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.db"
            # Crée un ancien schéma sans sender_email / current_folder / is_direct_or_action
            raw = sqlite3.connect(db_path)
            raw.executescript(
                """
                CREATE TABLE processed_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    message_id TEXT,
                    sender TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    short_title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    action_required TEXT,
                    notified_now INTEGER NOT NULL,
                    digested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(uidvalidity, uid)
                );
                CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            raw.commit()
            raw.close()

            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                now_iso = datetime.now(settings.timezone).isoformat()
                store.record_email(
                    uidvalidity=1,
                    uid=201,
                    message_id="<q1@insa-rouen.fr>",
                    sender="Prof A <profa@insa-rouen.fr>",
                    sender_email="profa@insa-rouen.fr",
                    subject="Stage PFE",
                    received_at=now_iso,
                    category="important",
                    short_title="Stage PFE",
                    summary="Merci de confirmer ton sujet.",
                    action_required="Répondre avant vendredi",
                    current_folder="INBOX",
                    is_direct_or_action=True,
                    notified_now=True,
                    digested=True,
                )
                store.record_email(
                    uidvalidity=1,
                    uid=202,
                    message_id="<q2@insa-rouen.fr>",
                    sender="Prof B <profb@insa-rouen.fr>",
                    sender_email="profb@insa-rouen.fr",
                    subject="Choix option",
                    received_at=now_iso,
                    category="important",
                    short_title="Choix option",
                    summary="Quel choix prends-tu ?",
                    action_required="Répondre",
                    current_folder="INBOX",
                    is_direct_or_action=True,
                    notified_now=True,
                    digested=True,
                )

                mailbox = MagicMock()
                mailbox.find_folder_by_role.return_value = "Sent"
                mailbox.fetch_recent_emails_since.return_value = (1, [])
                # Seul le mail <q1> a reçu une réponse
                mailbox.fetch_sent_reply_metadata.return_value = [
                    {
                        "in_reply_to": "<q1@insa-rouen.fr>",
                        "references": (),
                        "subject": "Re: Stage PFE",
                        "to": "profa@insa-rouen.fr",
                    }
                ]
                classifier = MailClassifier(settings)
                notifier = MagicMock()
                secretary = ImapSecretary(
                    settings, store, classifier, notifier, mailbox=mailbox
                )
                sent = secretary.maybe_send_daily_digest(force=True)
                self.assertTrue(sent)
                notifier.send_daily_digest.assert_called_once()
                args = notifier.send_daily_digest.call_args.args
                kwargs = notifier.send_daily_digest.call_args.kwargs
                self.assertEqual(args[0], 0)
                self.assertIn("Choix option", args[1])
                self.assertNotIn("Stage PFE", args[1])
                self.assertEqual(kwargs.get("pending_count"), 1)
                self.assertTrue(kwargs.get("preview"))
            finally:
                store.close()

    def test_recap_preview_does_not_cancel_evening_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                store.record_email(
                    uidvalidity=1,
                    uid=301,
                    message_id="<prev1@insa-rouen.fr>",
                    sender="BDE <bde@insa-rouen.fr>",
                    sender_email="bde@insa-rouen.fr",
                    subject="Forum campus",
                    received_at="2026-11-12T10:00:00+01:00",
                    category="newsletter",
                    short_title="Forum campus",
                    summary="Forum jeudi.",
                    action_required=None,
                    notified_now=False,
                    digested=False,
                )
                classifier = MagicMock()
                classifier.summarize_digest.return_value = "• Forum campus"
                notifier = MagicMock()
                mailbox = MagicMock()
                mailbox.find_folder_by_role.return_value = None
                mailbox.fetch_recent_emails_since.return_value = (1, [])
                secretary = ImapSecretary(
                    settings, store, classifier, notifier, mailbox=mailbox
                )

                # 15h00 : /recap en mode aperçu anticipé
                secretary.maybe_send_daily_digest(force=True)
                self.assertIsNone(store.get_last_digest_date())
                self.assertEqual(len(store.get_undigested_newsletters()), 1)
                notifier.send_daily_digest.assert_called_once_with(
                    1, "• Forum campus", preview=True
                )

                # 20h05 : le récap du soir s'envoie quand même
                after_target = datetime(2026, 11, 12, 20, 5, tzinfo=settings.timezone)
                with patch("src.imap_watcher.datetime") as mock_dt:
                    mock_dt.now.return_value = after_target
                    mock_dt.fromisoformat.side_effect = datetime.fromisoformat
                    sent = secretary.maybe_send_daily_digest(force=False)
                self.assertTrue(sent)
                self.assertEqual(store.get_undigested_newsletters(), [])
                self.assertEqual(store.get_last_digest_date(), "2026-11-12")
                self.assertEqual(notifier.send_daily_digest.call_count, 2)
                self.assertEqual(
                    notifier.send_daily_digest.call_args.args, (1, "• Forum campus")
                )
            finally:
                store.close()

    def test_digest_backfills_mails_since_yesterday_even_on_fresh_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                now_local = datetime(2026, 11, 12, 20, 5, tzinfo=settings.timezone)
                yesterday_dt = now_local - timedelta(hours=26)
                old_dt = now_local - timedelta(days=10)

                msg_yesterday = EmailMessage()
                msg_yesterday["From"] = "BDE <bde@insa-rouen.fr>"
                msg_yesterday["Subject"] = "Mail d'hier"
                msg_yesterday["Date"] = format_datetime(yesterday_dt)
                msg_yesterday["Message-ID"] = "<yest1@insa-rouen.fr>"
                msg_yesterday.set_content("Info reçue hier.")

                msg_too_old = EmailMessage()
                msg_too_old["From"] = "Ancien <ancien@insa-rouen.fr>"
                msg_too_old["Subject"] = "Vieux mail restauré"
                msg_too_old["Date"] = format_datetime(old_dt)
                msg_too_old["Message-ID"] = "<old10d@insa-rouen.fr>"
                msg_too_old.set_content("Ancien.")

                parsed_yest = parse_raw_email(401, msg_yesterday.as_bytes())
                parsed_old = parse_raw_email(402, msg_too_old.as_bytes())

                mailbox = MagicMock()
                mailbox.find_folder_by_role.return_value = None
                mailbox.fetch_recent_emails_since.return_value = (
                    1,
                    [parsed_yest, parsed_old],
                )
                classifier = MagicMock()
                classifier.classify.return_value = EmailClassification(
                    category="newsletter",
                    notify_now=False,
                    short_title="Mail d'hier",
                    summary="Info reçue hier.",
                    action_required=None,
                )
                classifier.summarize_digest.side_effect = (
                    lambda items, pending_replies=None: "\n".join(
                        f"• {it.short_title}" for it in items
                    )
                )
                notifier = MagicMock()
                secretary = ImapSecretary(
                    settings, store, classifier, notifier, mailbox=mailbox
                )

                with patch("src.imap_watcher.datetime") as mock_dt:
                    mock_dt.now.return_value = now_local
                    mock_dt.fromisoformat.side_effect = datetime.fromisoformat
                    sent = secretary.maybe_send_daily_digest(force=False)

                self.assertTrue(sent)
                notifier.send_urgent_alert.assert_not_called()
                notifier.send_daily_digest.assert_called_once()
                digest_body = notifier.send_daily_digest.call_args.args[1]
                self.assertIn("Mail d'hier", digest_body)
                self.assertNotIn("Vieux mail restauré", digest_body)
            finally:
                store.close()

    def test_system_metrics_status_and_startup_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "test.db"
            settings = _make_settings(db_path)
            store = StateStore(db_path)
            try:
                proc_dir = root / "proc"
                (proc_dir / "self").mkdir(parents=True)
                (proc_dir / "meminfo").write_text(
                    "MemTotal:        1024000 kB\n"
                    "MemFree:          300000 kB\n"
                    "MemAvailable:     716800 kB\n"
                    "SwapTotal:        512000 kB\n"
                    "SwapFree:         409600 kB\n",
                    encoding="utf-8",
                )
                (proc_dir / "uptime").write_text("9006.4 15000.0\n", encoding="utf-8")
                (proc_dir / "self" / "status").write_text(
                    "Name:\tpython\nVmRSS:\t   47104 kB\n", encoding="utf-8"
                )

                metrics = collect_system_metrics(db_path, proc_dir=proc_dir)
                self.assertEqual(metrics.ram_total_mb, 1000.0)
                self.assertEqual(metrics.ram_available_mb, 700.0)
                self.assertEqual(metrics.ram_used_mb, 300.0)
                self.assertEqual(metrics.swap_total_mb, 500.0)
                self.assertEqual(metrics.swap_used_mb, 100.0)
                self.assertEqual(metrics.bot_rss_mb, 46.0)
                self.assertAlmostEqual(metrics.system_uptime_seconds or 0.0, 9006.4)

                status_text = format_vps_status(
                    metrics,
                    bot_uptime_seconds=3720,
                    started_at_str="12/11 à 14:00",
                    settings=settings,
                    stats=store.get_stats(),
                )
                self.assertIn("300 / 1000 Mo (30.0 %", status_text)
                self.assertIn("Swap :** 100 / 500 Mo", status_text)
                self.assertIn("RAM processus bot :** 46.0 Mo", status_text)
                self.assertIn("Uptime VPS :** 2h30m", status_text)

                notifier = TelegramNotifier(settings)
                with patch("src.notifier.requests.post") as mock_post:
                    mock_post.return_value = MagicMock(ok=True, status_code=200)
                    notifier.send_startup_message(settings, metrics=metrics)
                sent_text = mock_post.call_args.kwargs["json"]["text"]
                self.assertIn("redémarré et actif", sent_text)
                self.assertIn("300 / 1000 Mo", sent_text)
                self.assertIn("46.0 Mo", sent_text)

                mock_notifier = MagicMock()
                loop = TelegramAssistantLoop(
                    settings, store, mock_notifier, MagicMock()
                )
                loop.handle_update(
                    {"message": {"chat": {"id": 999}, "text": "/status"}}
                )
                mock_notifier.send_plain_reply.assert_called_once()
                reply_text = mock_notifier.send_plain_reply.call_args.args[0]
                self.assertIn("État du secrétaire & VPS", reply_text)
                self.assertIn("Disque :", reply_text)
            finally:
                store.close()

    def test_search_folder_ranks_multi_keyword_matches_first_and_skips_pdf_oom(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            tools = ImapMailboxTools(settings)

            msg_hit_all = EmailMessage()
            msg_hit_all["From"] = "etudiants@insa-rouen.fr"
            msg_hit_all["Subject"] = "Semaine bien-être : découvrez le programme !"
            msg_hit_all.set_content("Ateliers bien-être toute la semaine.")

            msg_noise = EmailMessage()
            msg_noise["From"] = "prof@insa-rouen.fr"
            msg_noise["Subject"] = "TP"
            msg_noise.set_content("Bien cordialement.")

            env_hit_all = MagicMock(
                date=datetime(2026, 11, 12, 13, 46, tzinfo=settings.timezone)
            )
            env_noise_newer = MagicMock(
                date=datetime(2026, 11, 12, 14, 10, tzinfo=settings.timezone)
            )

            with patch("src.imap_tools.IMAPClient") as mock_imap_cls:
                mock_imap = mock_imap_cls.return_value
                # 'bien' matche 5473 et 5500; 'etre' et 'semaine' matchent 5473
                mock_imap.search.side_effect = [
                    [5473, 5500],
                    [5473],
                    [5473],
                ]
                mock_imap.fetch.side_effect = [
                    {
                        5473: {b"ENVELOPE": env_hit_all},
                        5500: {b"ENVELOPE": env_noise_newer},
                    },
                    {
                        5473: {b"BODY[]<0>": msg_hit_all.as_bytes()},
                        5500: {b"BODY[]<0>": msg_noise.as_bytes()},
                    },
                ]
                results = tools.search_folder(
                    folder="INBOX", keywords=["bien", "etre", "semaine"], max_results=2
                )
                self.assertEqual([r.uid for r in results], [5473, 5500])

    def test_classifier_cooldown_on_429_and_prefer_fast_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir) / "test.db")
            settings = Settings(
                **{
                    **settings.__dict__,
                    "gemini_model": "gemini-3.5-flash",
                    "gemini_fallback_models": (
                        "gemini-3.5-flash-lite",
                        "gemini-3.1-flash-lite",
                    ),
                }
            )
            with patch("src.classifier.genai.Client") as mock_client_cls:
                mock_client = mock_client_cls.return_value
                ok_resp = MagicMock()
                ok_resp.text = "OK"

                # 1) prefer_fast=True doit choisir -lite en premier (préserve le quota 20/j)
                mock_client.models.generate_content.return_value = ok_resp
                clf = MailClassifier(settings)
                clf.generate_with_fallback("hello", {"temperature": 0.1}, prefer_fast=True)
                self.assertEqual(
                    mock_client.models.generate_content.call_args.kwargs["model"],
                    "gemini-3.5-flash-lite",
                )

                # 2) prefer_fast=False : 429 sur 3.5-flash met le modèle en cooldown
                mock_client.models.generate_content.reset_mock()
                mock_client.models.generate_content.side_effect = [
                    RuntimeError("429 RESOURCE_EXHAUSTED"),
                    ok_resp,
                    ok_resp,
                ]
                clf.generate_with_fallback("q1", {"temperature": 0.1})
                clf.generate_with_fallback("q2", {"temperature": 0.1})
                called_models = [
                    c.kwargs["model"]
                    for c in mock_client.models.generate_content.call_args_list
                ]
                # q1 tente 3.5-flash (429) puis 3.5-flash-lite ; q2 saute directement à 3.5-flash-lite
                self.assertEqual(
                    called_models,
                    ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"],
                )


if __name__ == "__main__":
    unittest.main()
