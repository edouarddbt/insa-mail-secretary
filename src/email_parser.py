"""Extraction propre des en-têtes et du contenu texte d'un email brut RFC 5322."""

from __future__ import annotations

import email
import email.policy
import io
import logging
import re
from dataclasses import dataclass, field
from email.headerregistry import AddressHeader
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

logger = logging.getLogger(__name__)
MAX_PDF_BYTES = 5 * 1024 * 1024


class _HTMLTextExtractor(HTMLParser):
    """Convertit du HTML en texte brut lisible en ignorant scripts et styles."""

    _SKIP_TAGS = {"script", "style", "head", "noscript"}
    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag_lower in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag_lower in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return re.sub(r"\n{3,}", "\n\n", cleaned)


def html_to_text(html_content: str) -> str:
    """Extrait le texte lisible d'un fragment ou document HTML."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html_content)
    extractor.close()
    return extractor.get_text()


def _format_addresses(header: object) -> tuple[str, ...]:
    if isinstance(header, AddressHeader):
        return tuple(
            addr.addr_spec for addr in header.addresses if getattr(addr, "addr_spec", "")
        )
    if header:
        return (str(header),)
    return ()


def extract_pdf_text(
    pdf_bytes: bytes, max_chars: int = 3000, max_pages: int = 8
) -> str:
    """Extrait le texte d'une pièce jointe PDF sans saturer la mémoire."""
    if not pdf_bytes or len(pdf_bytes) > MAX_PDF_BYTES:
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            extracted = (page.extract_text() or "").strip()
            if extracted:
                chunks.append(extracted)
            if sum(len(c) for c in chunks) >= max_chars:
                break
        text = "\n\n".join(chunks).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n[... PDF tronqué ...]"
        return text
    except Exception as exc:
        logger.debug("Impossible de lire un PDF joint : %s", exc)
        return ""


def _extract_body_and_attachments(
    msg: EmailMessage,
    max_chars: int = 4500,
    *,
    extract_pdfs: bool = True,
) -> tuple[str, tuple[str, ...]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    pdf_excerpts: list[str] = []
    attachment_names: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            ctype = (part.get_content_type() or "").lower()
            filename = (part.get_filename() or "").strip()

            is_pdf = ctype == "application/pdf" or filename.lower().endswith(".pdf")
            if is_pdf:
                label = filename or "document.pdf"
                attachment_names.append(label)
                if extract_pdfs and len(pdf_excerpts) < 2:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        pdf_text = extract_pdf_text(payload)
                        if pdf_text:
                            pdf_excerpts.append(
                                f"[Pièce jointe PDF : {label}]\n{pdf_text}"
                            )
                continue

            if disposition == "attachment":
                if filename:
                    attachment_names.append(filename)
                continue

            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                content = (
                    payload.decode(charset, errors="replace")
                    if isinstance(payload, bytes)
                    else ""
                )
            if not isinstance(content, str):
                continue
            if ctype == "text/plain":
                plain_parts.append(content)
            elif ctype == "text/html":
                html_parts.append(content)
    else:
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            content = (
                payload.decode(charset, errors="replace")
                if isinstance(payload, bytes)
                else ""
            )
        if isinstance(content, str):
            if msg.get_content_type() == "text/html":
                html_parts.append(content)
            else:
                plain_parts.append(content)

    text = "\n\n".join(p.strip() for p in plain_parts if p.strip())
    if not text and html_parts:
        text = html_to_text("\n\n".join(html_parts))

    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[... message tronqué ...]"

    if pdf_excerpts:
        pdf_block = "\n\n".join(pdf_excerpts)
        text = f"{text}\n\n{pdf_block}".strip() if text else pdf_block

    return text, tuple(attachment_names)


@dataclass(frozen=True)
class ParsedEmail:
    """Représentation structurée et nettoyée d'un mail entrant."""

    uid: int
    message_id: str | None
    sender_name: str
    sender_email: str
    to_addrs: tuple[str, ...]
    cc_addrs: tuple[str, ...]
    subject: str
    date_iso: str
    body_text: str
    is_bulk_list: bool
    list_id: str | None
    attachment_names: tuple[str, ...] = field(default_factory=tuple)
    in_reply_to: str | None = None
    references: tuple[str, ...] = field(default_factory=tuple)

    @property
    def sender_display(self) -> str:
        if self.sender_name and self.sender_name.lower() != self.sender_email.lower():
            return f"{self.sender_name} <{self.sender_email}>"
        return self.sender_email or self.sender_name or "Expéditeur inconnu"


MAX_PARSE_RAW_BYTES = 8 * 1024 * 1024


def parse_raw_email(
    uid: int,
    raw_bytes: bytes,
    *,
    extract_pdfs: bool = True,
) -> ParsedEmail:
    """Parse les octets bruts d'un email récupéré par IMAP (protégé contre les OOM)."""
    safe_bytes = (
        raw_bytes[:262144] if len(raw_bytes) > MAX_PARSE_RAW_BYTES else raw_bytes
    )
    msg = email.message_from_bytes(safe_bytes, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    from_header = msg.get("From")
    sender_name = ""
    sender_email = ""
    if isinstance(from_header, AddressHeader) and from_header.addresses:
        first_addr = from_header.addresses[0]
        sender_name = (first_addr.display_name or "").strip()
        sender_email = (first_addr.addr_spec or "").strip().lower()
    elif from_header:
        sender_email = str(from_header).strip()

    subject = str(msg.get("Subject") or "(Sans objet)").strip()
    message_id = str(msg.get("Message-ID") or "").strip() or None

    raw_date = msg.get("Date")
    date_iso = ""
    if raw_date:
        try:
            date_iso = parsedate_to_datetime(str(raw_date)).isoformat()
        except Exception:
            date_iso = str(raw_date)

    precedence = str(msg.get("Precedence") or "").strip().lower()
    list_unsubscribe = msg.get("List-Unsubscribe")
    list_id_raw = str(msg.get("List-Id") or "").strip() or None
    is_bulk_list = bool(
        list_unsubscribe
        or list_id_raw
        or precedence in {"bulk", "list", "junk"}
    )
    in_reply_to = str(msg.get("In-Reply-To") or "").strip() or None
    refs_raw = str(msg.get("References") or "").strip()
    references = tuple(r.strip() for r in refs_raw.split() if r.strip())
    body_text, attachment_names = _extract_body_and_attachments(
        msg, extract_pdfs=extract_pdfs
    )

    return ParsedEmail(
        uid=uid,
        message_id=message_id,
        sender_name=sender_name,
        sender_email=sender_email,
        to_addrs=_format_addresses(msg.get("To")),
        cc_addrs=_format_addresses(msg.get("Cc")),
        subject=subject,
        date_iso=date_iso,
        body_text=body_text,
        is_bulk_list=is_bulk_list,
        list_id=list_id_raw,
        attachment_names=attachment_names,
        in_reply_to=in_reply_to,
        references=references,
    )
