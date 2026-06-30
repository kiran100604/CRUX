"""Turn an external source — a URL, a PDF, an HTML/markdown/text file — into plain
text the normal ingest pipeline can chunk → extract → place in the KB tree.

Kept dependency-light: PDFs use `pypdf` if present (optional extra) and degrade to
a clear message otherwise; HTML is stripped with the stdlib; URL fetch uses urllib
so there's no hard `requests` dependency. Everything returns plain text plus a
short, human title so the episode is labelled.
"""

from __future__ import annotations

import html as _html
import io
import re
import urllib.request
from html.parser import HTMLParser


class SourceError(Exception):
    """A source couldn't be turned into text — message is safe to show the user."""


# --- HTML → text -------------------------------------------------------------

class _Stripper(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("# ")          # keep headings so chunking can nest

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    p = _Stripper()
    try:
        p.feed(raw)
    except Exception:
        pass
    text = _html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    return text, p.title.strip()


# --- PDF → text --------------------------------------------------------------

def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except BaseException as e:  # noqa: BLE001 - optional dep; a broken/missing
        # install (incl. native-extension panics) must degrade, never crash ingest
        raise SourceError(
            "PDF support needs pypdf — install it with: pip install 'crux[pdf]'") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(pg.extract_text() or "") for pg in reader.pages]
    except Exception as e:
        raise SourceError(f"couldn't read that PDF ({type(e).__name__})") from e
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text.strip():
        raise SourceError("that PDF has no extractable text (scanned image?)")
    return text


# --- dispatch ----------------------------------------------------------------

def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """(filename, bytes) → (text, title). Picks a parser by extension; defaults to
    decoding as UTF-8 text. Raises SourceError with a user-safe message on failure."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return pdf_to_text(data), _stem(filename)
    decoded = data.decode("utf-8", errors="ignore")
    if name.endswith((".html", ".htm")):
        text, title = html_to_text(decoded)
        return text, (title or _stem(filename))
    if not decoded.strip():
        raise SourceError("that file is empty or not text")
    return decoded, _stem(filename)


def fetch_url(url: str, *, timeout: float = 20.0, max_bytes: int = 4_000_000) -> tuple[str, str]:
    """Fetch a URL and return (text, title). HTML is stripped to text; a raw .pdf
    link is parsed as a PDF; anything else is treated as text. Network/decoding
    failures raise SourceError with a message safe to show the user."""
    if not re.match(r"^https?://", url.strip(), re.I):
        raise SourceError("enter a full http(s):// URL")
    req = urllib.request.Request(url, headers={"User-Agent": "CRUX/0.2 (+knowledge ingest)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(max_bytes + 1)
    except Exception as e:
        raise SourceError(f"couldn't fetch that URL ({type(e).__name__}: {str(e)[:80]})") from e
    if len(data) > max_bytes:
        data = data[:max_bytes]
    if "application/pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return pdf_to_text(data), _url_title(url)
    decoded = data.decode("utf-8", errors="ignore")
    if "html" in ctype or re.search(r"<html|<!doctype html", decoded[:2000], re.I):
        text, title = html_to_text(decoded)
        if not text.strip():
            raise SourceError("that page had no readable text")
        return text, (title or _url_title(url))
    if not decoded.strip():
        raise SourceError("that URL returned no text")
    return decoded, _url_title(url)


def _stem(filename: str) -> str:
    base = re.sub(r"\.[A-Za-z0-9]+$", "", (filename or "").split("/")[-1])
    return base.replace("_", " ").replace("-", " ").strip()[:80] or "Uploaded file"


def _url_title(url: str) -> str:
    m = re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")
    return m[:80] or url
