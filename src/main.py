"""Point d'entrée CLI du secrétaire IA INSA Rouen."""

from __future__ import annotations

import argparse
import logging
import sys

from src.bot_assistant import MailSearchService, TelegramAssistantLoop
from src.classifier import MailClassifier
from src.config import Settings
from src.email_parser import ParsedEmail, parse_raw_email
from src.imap_watcher import ImapSecretary
from src.notifier import TelegramNotifier
from src.storage import StateStore


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _cmd_test_imap(settings: Settings) -> int:
    settings.validate_imap()
    from imapclient import IMAPClient

    print(f"Connexion à {settings.imap_host}:{settings.imap_port} (SSL/TLS)...")
    client = IMAPClient(settings.imap_host, port=settings.imap_port, ssl=True, timeout=20)
    try:
        client.login(settings.imap_user, settings.imap_password)
        info = client.select_folder(settings.imap_folder, readonly=True)
        exists = int(info.get(b"EXISTS", 0))
        uidvalidity = int(info.get(b"UIDVALIDITY", 0))
        has_idle = client.has_capability(b"IDLE")
        print(
            f"Connexion réussie ! Dossier={settings.imap_folder}, "
            f"Messages={exists}, UIDVALIDITY={uidvalidity}, IMAP IDLE={'oui' if has_idle else 'non'}"
        )
        uids = sorted(int(u) for u in client.search(["ALL"]))
        if uids:
            latest_uid = uids[-1]
            data = client.fetch([latest_uid], ["BODY.PEEK[]"])[latest_uid]
            raw_bytes = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
            if isinstance(raw_bytes, bytes):
                mail = parse_raw_email(latest_uid, raw_bytes)
                print(f"Dernier mail (UID={latest_uid}) : {mail.sender_display} | {mail.subject}")
    finally:
        client.logout()
    return 0


def _cmd_test_telegram(settings: Settings) -> int:
    settings.validate_telegram()
    notifier = TelegramNotifier(settings)
    bot_name = notifier.verify_bot()
    print(f"Bot Telegram vérifié : @{bot_name}")
    notifier.send_test_message()
    print("Message de test envoyé sur Telegram !")
    return 0


def _cmd_test_gemini(settings: Settings) -> int:
    settings.validate_gemini()
    classifier = MailClassifier(settings)
    sample = ParsedEmail(
        uid=1,
        message_id="<test@insa-rouen.fr>",
        sender_name="Scolarité INSA Rouen",
        sender_email="scolarite@insa-rouen.fr",
        to_addrs=(settings.imap_user or "etudiant@insa-rouen.fr",),
        cc_addrs=(),
        subject="Modification salle examen Analyse Numérique - Demain 08h30",
        date_iso="2026-11-12T10:00:00+01:00",
        body_text=(
            "Bonjour, l'épreuve d'Analyse Numérique prévue demain à 08h30 aura lieu "
            "en amphithéâtre D. Merci d'apporter votre carte étudiante."
        ),
        is_bulk_list=False,
        list_id=None,
    )
    res = classifier.classify(sample, raise_on_error=True)
    print("Résultat Gemini :", res.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Secrétaire IA pour messagerie IMAP INSA Rouen -> Telegram"
    )
    parser.add_argument("--env", default=None, help="Chemin vers un fichier .env")
    parser.add_argument("--verbose", "-v", action="store_true", help="Logs détaillés")
    parser.add_argument("--test-imap", action="store_true", help="Tester la connexion IMAP")
    parser.add_argument("--test-telegram", action="store_true", help="Tester le bot Telegram")
    parser.add_argument("--test-gemini", action="store_true", help="Tester l'API Gemini")
    parser.add_argument(
        "--once", action="store_true", help="Synchroniser une seule fois puis quitter"
    )
    parser.add_argument(
        "--run-digest-now",
        action="store_true",
        help="Envoyer immédiatement le récap des mails en attente",
    )
    parser.add_argument(
        "--ask",
        type=str,
        default=None,
        help="Poser une question et chercher la réponse dans les mails",
    )
    args = parser.parse_args(argv)

    _setup_logging(verbose=args.verbose)
    settings = Settings.from_env(args.env)

    if args.test_imap:
        return _cmd_test_imap(settings)
    if args.test_telegram:
        return _cmd_test_telegram(settings)
    if args.test_gemini:
        return _cmd_test_gemini(settings)

    settings.validate_all()
    store = StateStore(settings.db_path)
    try:
        classifier = MailClassifier(settings)
        notifier = TelegramNotifier(settings)
        search_service = MailSearchService(settings, classifier)

        if args.ask:
            print(search_service.answer_question(args.ask))
            return 0

        secretary = ImapSecretary(settings, store, classifier, notifier)

        if args.run_digest_now:
            secretary.maybe_send_daily_digest(force=True)
            return 0
        if args.once:
            count = secretary.run_once()
            print(f"{count} nouveau(x) mail(s) traité(s).")
            return 0

        assistant_loop = TelegramAssistantLoop(
            settings=settings,
            store=store,
            notifier=notifier,
            search_service=search_service,
            trigger_digest_cb=lambda: secretary.maybe_send_daily_digest(force=True),
        )
        assistant_loop.start()
        try:
            notifier.send_startup_message(settings)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Impossible d'envoyer la notification de démarrage Telegram: %s", exc
            )
        try:
            secretary.run_forever()
        finally:
            assistant_loop.stop()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
