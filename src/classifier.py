"""Classification et résumé des mails via l'API Gemini (Google GenAI SDK)."""

from __future__ import annotations

import logging
import time
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from src.config import Settings
from src.email_parser import ParsedEmail
from src.storage import StoredNewsletter

logger = logging.getLogger(__name__)


class CalendarEvent(BaseModel):
    """Événement agenda extrait d'un mail s'il mentionne une date/heure précise."""

    title: str = Field(description="Titre court de l'événement (ex: 'Réunion mobilité Spring 2027').")
    start_iso: str = Field(
        description="Date/heure de début au format ISO local YYYY-MM-DDTHH:MM:SS ou YYYY-MM-DD si journée entière."
    )
    end_iso: str | None = Field(
        default=None,
        description="Date/heure de fin ISO si connue, sinon null.",
    )
    location: str | None = Field(
        default=None,
        description="Lieu ou lien visio (ex: 'Amphi D' ou 'Teams'), sinon null.",
    )
    details: str | None = Field(
        default=None,
        description="Bref rappel utile (1 phrase), sinon null.",
    )


class EmailClassification(BaseModel):
    """Sortie structurée retournée par Gemini pour un mail entrant."""

    category: Literal["urgent", "important", "newsletter", "spam"] = Field(
        description=(
            "urgent = action immédiate requise / convocation / problème bloquant / deadline proche ; "
            "important = mail personnel, professeur, scolarité, stage, projet, ou qui concerne directement l'utilisateur ; "
            "newsletter = diffusion générale école, association, événement campus, offre générique ; "
            "spam = publicité ou message sans intérêt."
        )
    )
    notify_now: bool = Field(
        description="True uniquement si l'utilisateur doit recevoir une alerte sur son téléphone tout de suite."
    )
    short_title: str = Field(
        description="Titre très court en français résumant l'objet réel (max 80 caractères)."
    )
    summary: str = Field(
        description="Résumé clair en français en 1 à 3 phrases avec les infos clés (dates, lieu, contexte)."
    )
    action_required: str | None = Field(
        default=None,
        description="Action concrète attendue de l'utilisateur (ex: 'Répondre avant vendredi 14h'), ou null si aucune.",
    )
    calendar_event: CalendarEvent | None = Field(
        default=None,
        description="Renseigner uniquement si le mail mentionne une réunion, un examen, une soutenance ou une date limite précise à ajouter à l'agenda.",
    )


class SearchPlan(BaseModel):
    """Plan de recherche IMAP extrait d'une question utilisateur."""

    keywords: list[str] = Field(
        default_factory=list,
        description="1 à 4 mots-clés simples et pertinents à chercher dans les mails (ex: ['stage', 'convention']).",
    )
    sender_query: str | None = Field(
        default=None,
        description="Nom ou adresse de l'expéditeur si mentionné dans la question, sinon null.",
    )
    days_back: int | None = Field(
        default=None,
        description="Horizon temporel estimé en jours (ex: 7, 30, 180) si la question mentionne une période, sinon null.",
    )


def _matches_rule(sender_email: str, patterns: tuple[str, ...]) -> bool:
    sender_lower = sender_email.lower()
    return any(pat in sender_lower for pat in patterns)


class MailClassifier:
    """Encapsule les appels à Gemini pour trier les mails et générer le récap du soir."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Timeout strict (25s) et 1 seule tentative par modèle : en cas de 503/429,
        # generate_with_fallback bascule instantanément sur le modèle de secours.
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=25000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self._cooldown_until: dict[str, float] = {}

    @property
    def client(self) -> genai.Client:
        """Retourne le client GenAI sous-jacent."""
        return self._client

    def _model_candidates(self, *, prefer_fast: bool = False) -> list[str]:
        raw = [self.settings.gemini_model, *self.settings.gemini_fallback_models]
        unique = list(dict.fromkeys(m for m in raw if m))
        if prefer_fast:
            # Préserve le quota limité (20 req/j) des modèles complets pour le chat Q&A
            unique.sort(key=lambda m: 0 if "lite" in m.lower() else 1)
        now = time.monotonic()
        active = [m for m in unique if self._cooldown_until.get(m, 0.0) <= now]
        return active or unique

    def generate_with_fallback(
        self, contents: object, config: object, *, prefer_fast: bool = False
    ) -> object:
        """Exécute generate_content avec bascule automatique et cooldown sur 429."""
        last_error: Exception | None = None
        for model_name in self._model_candidates(prefer_fast=prefer_fast):
            try:
                return self._client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                last_error = exc
                err_msg = str(exc)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    self._cooldown_until[model_name] = time.monotonic() + 1800
                logger.warning(
                    "Modèle %s indisponible (%s), essai suivant...", model_name, exc
                )
        assert last_error is not None
        raise last_error

    def classify(
        self,
        mail: ParsedEmail,
        *,
        raise_on_error: bool = False,
        extra_muted: tuple[str, ...] = (),
    ) -> EmailClassification:
        """Classe un email entrant selon son urgence et le contexte utilisateur."""
        all_muted = (*self.settings.muted_senders, *extra_muted)
        if not raise_on_error and _matches_rule(mail.sender_email, all_muted):
            snippet = (mail.body_text or mail.subject).splitlines()[0][:180]
            return EmailClassification(
                category="newsletter",
                notify_now=False,
                short_title=mail.subject[:80],
                summary=snippet,
                action_required=None,
                calendar_event=None,
            )

        is_vip = _matches_rule(mail.sender_email, self.settings.vip_senders)
        addressed_directly = self.settings.imap_user.lower() in mail.to_addrs

        prompt = (
            "Tu es un secrétaire personnel intelligent qui trie les emails universitaires.\n"
            f"Contexte sur l'utilisateur : {self.settings.user_context}\n\n"
            "Règles de décision :\n"
            "- Classe en 'urgent' ou 'important' (avec notify_now=true) tout message d'un professeur, "
            "de la scolarité, de l'administration concernant directement l'étudiant, d'un encadrant de stage/alternance, "
            "d'un camarade/projet, une convocation, une modification de cours/examen, ou un mail adressé personnellement.\n"
            "- Classe en 'newsletter' (avec notify_now=false) les annonces générales de campus, associations étudiantes, "
            "BDE, conférences ouvertes à tous, offres d'emploi génériques, infolettres.\n"
            "- Classe en 'spam' (avec notify_now=false) les démarchages commerciaux ou bruits inutiles.\n"
            "- En cas de doute sur un potentiel impact scolaire/administratif, privilégie notify_now=true.\n"
            "- Si le mail contient une date précise d'examen, de réunion, de soutenance ou une deadline, remplis `calendar_event`.\n\n"
            "Métadonnées du mail :\n"
            f"- Expéditeur : {mail.sender_display}\n"
            f"- Objet : {mail.subject}\n"
            f"- Date : {mail.date_iso}\n"
            f"- Adressé directement à l'utilisateur en To : {'oui' if addressed_directly else 'non'}\n"
            f"- En-têtes liste de diffusion / désinscription : {'oui' if mail.is_bulk_list else 'non'}\n"
            f"- Expéditeur VIP configuré : {'oui' if is_vip else 'non'}\n\n"
            "Contenu du mail :\n"
            f"{mail.body_text or '(Contenu vide)'}"
        )

        try:
            response = self.generate_with_fallback(
                prompt,
                {
                    "response_mime_type": "application/json",
                    "response_schema": EmailClassification,
                    "temperature": 0.1,
                },
                prefer_fast=True,
            )
            result: EmailClassification | None = getattr(response, "parsed", None)
            if result is None:
                raise ValueError("Réponse structurée vide de Gemini")
        except Exception as exc:
            logger.exception("Erreur Gemini lors de la classification : %s", exc)
            if raise_on_error:
                raise
            # Repli sûr : on préfère notifier un mail non-bulk plutôt que rater une urgence
            fallback_notify = is_vip or not mail.is_bulk_list
            fallback_snippet = (mail.body_text or "(Aperçu indisponible)")[:280]
            return EmailClassification(
                category="important" if fallback_notify else "newsletter",
                notify_now=fallback_notify,
                short_title=mail.subject[:80],
                summary=fallback_snippet,
                action_required=None,
            )

        if is_vip and not result.notify_now:
            return EmailClassification(
                category="important"
                if result.category in {"newsletter", "spam"}
                else result.category,
                notify_now=True,
                short_title=result.short_title,
                summary=result.summary,
                action_required=result.action_required,
                calendar_event=result.calendar_event,
            )

        return result

    def draft_reply(self, mail: ParsedEmail) -> str:
        """Génère un brouillon de réponse poli et concis en français."""
        prompt = (
            "Tu es l'assistant de rédaction d'un étudiant de l'INSA Rouen.\n"
            f"Contexte utilisateur : {self.settings.user_context}\n"
            "Rédige un brouillon de réponse courtois, naturel et concis en français à ce mail.\n"
            "- Commence par une formule d'appel adaptée.\n"
            "- Si une information manque (ex: disponibilité exacte), laisse un marqueur clair entre crochets `[à compléter]`.\n"
            "- Ne mets pas d'objet sauf si utile, et n'utilise aucune balise HTML.\n\n"
            f"De : {mail.sender_display}\n"
            f"Objet : {mail.subject}\n"
            f"Contenu :\n{mail.body_text or '(Vide)'}"
        )
        try:
            response = self.generate_with_fallback(
                prompt, {"temperature": 0.3}, prefer_fast=True
            )
            text = (getattr(response, "text", None) or "").strip()
            if text:
                return text
        except Exception as exc:
            logger.exception("Erreur Gemini lors du brouillon de réponse : %s", exc)
        return (
            f"Bonjour,\n\nMerci pour votre message concernant « {mail.subject} ».\n"
            "[Compléter votre réponse ici]\n\nCordialement,\n[Votre nom]"
        )

    def summarize_digest(self, items: list[StoredNewsletter]) -> str:
        """Génère le récapitulatif du soir des mails secondaires/newsletters."""
        if not items:
            return "Aucun mail secondaire en attente aujourd'hui."

        lines = [
            f"{idx}. Expéditeur: {item.sender} | Objet: {item.subject} | Résumé: {item.summary}"
            for idx, item in enumerate(items, start=1)
        ]
        joined = "\n".join(lines[:80])
        prompt = (
            "Tu es le secrétaire IA de l'utilisateur. Voici les mails secondaires / newsletters reçus aujourd'hui "
            "qui n'ont pas déclenché de notification en direct.\n"
            "Rédige un mini récapitulatif clair et agréable en français pour le soir.\n"
            "Consignes de mise en forme (lecture sur téléphone) :\n"
            "- Regroupe par petits thèmes avec un emoji et un titre en gras (ex: 🎉 **Campus & Assos**, 📚 **Infos école**, 💼 **Stages / Carrière**).\n"
            "- Pour chaque mail utile, fais 1 seule puce `- ` très courte (15-25 mots max) avec l'info essentielle et la date/heure.\n"
            "- Saute une ligne entre chaque puce et entre chaque rubrique pour que ce soit très aéré.\n"
            "- Fusionne ou ignore les pur spams sans intérêt (en indiquant juste combien ont été ignorés s'il y en a).\n"
            "- N'utilise PAS de balises HTML : uniquement du texte simple avec `**gras**` et des puces `- `.\n\n"
            f"Mails du jour ({len(items)} au total) :\n{joined}"
        )
        try:
            response = self.generate_with_fallback(
                prompt, {"temperature": 0.2}, prefer_fast=True
            )
            text = (getattr(response, "text", None) or "").strip()
            if text:
                return text
        except Exception as exc:
            logger.exception("Erreur Gemini lors de la génération du récap : %s", exc)

        return "\n".join(
            f"• **{item.short_title}** ({item.sender})"
            for item in items[:30]
        )

    def plan_email_search(self, question: str) -> SearchPlan:
        """Transforme une question libre en mots-clés de recherche IMAP."""
        prompt = (
            "Extrais les critères de recherche pour retrouver des emails répondant à cette question.\n"
            "Donne uniquement des mots simples (sans phrases, de préférence au singulier) utiles pour une recherche IMAP.\n"
            f"Question : {question}"
        )
        try:
            response = self.generate_with_fallback(
                prompt,
                {
                    "response_mime_type": "application/json",
                    "response_schema": SearchPlan,
                    "temperature": 0.0,
                },
            )
            plan: SearchPlan | None = getattr(response, "parsed", None)
            if plan is not None:
                return plan
        except Exception as exc:
            logger.warning("Échec planification recherche Gemini (%s), repli basique.", exc)

        words = [
            w.strip(".,?!'\"()[]{}:;")
            for w in question.split()
            if len(w.strip(".,?!'\"()[]{}:;")) >= 4
        ]
        return SearchPlan(keywords=words[:3], sender_query=None, days_back=None)

    def answer_question_from_emails(
        self, question: str, emails: list[ParsedEmail]
    ) -> str:
        """Répond à une question utilisateur uniquement à partir des mails trouvés."""
        if not emails:
            return (
                "Je n'ai trouvé aucun mail correspondant à cette recherche dans ta boîte INSA."
            )

        context_blocks = []
        for idx, mail in enumerate(emails, start=1):
            snippet = (mail.body_text or "")[:1800]
            context_blocks.append(
                f"--- MAIL {idx} ---\n"
                f"Date : {mail.date_iso or 'inconnue'}\n"
                f"De : {mail.sender_display}\n"
                f"Objet : {mail.subject}\n"
                f"Contenu :\n{snippet}"
            )

        prompt = (
            "Tu es l'assistant mail INSA Rouen de l'utilisateur.\n"
            "Réponds précisément en français à sa question en te basant UNIQUEMENT sur les mails ci-dessous.\n"
            "Règles :\n"
            "- Si l'information est présente, donne la réponse claire dès la première phrase, puis cite le mail source (Date, Expéditeur, Objet).\n"
            "- Si aucun des mails fournis ne contient la réponse, dis-le honnêtement sans rien inventer.\n"
            "- N'utilise pas de balises HTML ; reste concis et lisible sur mobile.\n\n"
            f"Question : {question}\n\n"
            + "\n\n".join(context_blocks)
        )

        try:
            response = self.generate_with_fallback(prompt, {"temperature": 0.1})
            text = (getattr(response, "text", None) or "").strip()
            if text:
                return text
        except Exception as exc:
            logger.exception("Erreur Gemini lors de la réponse Q&A : %s", exc)

        fallback_lines = ["Voici les mails trouvés (résumé IA indisponible) :"]
        for mail in emails[:5]:
            fallback_lines.append(f"• {mail.date_iso[:10]} — {mail.sender_display} : {mail.subject}")
        return "\n".join(fallback_lines)
