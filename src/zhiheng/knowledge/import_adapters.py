"""Deterministic import adapters with explicit failure and refresh semantics."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ImportAdapterError(ValueError):
    """A user-facing adapter failure with stable machine-readable semantics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        retryable: bool,
        diagnostic_id: str | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.diagnostic_id = diagnostic_id or code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ImportResult:
    title: str
    text: str
    media_type: str
    source_metadata: dict[str, Any]

    @property
    def content_sha256(self) -> str:
        return web_content_sha256(self.text)


@dataclass(frozen=True)
class OCRTextBlock:
    text: str
    confidence: float | None = None
    page_no: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class OCRExtraction:
    text: str
    language: str | None = None
    blocks: tuple[OCRTextBlock, ...] = ()
    provider: str = "unknown"


class OCRProvider(Protocol):
    """Replaceable OCR provider contract used by the asynchronous job worker."""

    def extract(self, data: bytes, *, title: str) -> OCRExtraction: ...


class UnavailableOCRProvider:
    """Default provider until a deployment injects a concrete OCR implementation."""

    def extract(self, data: bytes, *, title: str) -> OCRExtraction:
        del data, title
        raise ImportAdapterError(
            "ocr_provider_unavailable",
            "unsupported: OCR provider is not configured",
            stage="ocr",
            retryable=True,
            diagnostic_id="ocr-provider-missing",
        )


def parse_markdown(data: bytes, *, title: str) -> ImportResult:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportAdapterError(
            "parse_failed",
            "Markdown must be UTF-8 encoded",
            stage="parse",
            retryable=False,
        ) from exc
    return ImportResult(title, text, "text/markdown", {"format": "markdown"})


def parse_pdf(data: bytes, *, title: str) -> ImportResult:
    """Keep the legacy adapter explicit while PDF parsing runs asynchronously."""

    del title
    if not data.startswith(b"%PDF"):
        raise ImportAdapterError(
            "parse_failed",
            "invalid PDF header",
            stage="parse",
            retryable=False,
        )
    raise ImportAdapterError(
        "parse_failed",
        "PDF parsing is asynchronous; submit the file to /v1/knowledge/pdf-imports",
        stage="parse",
        retryable=False,
    )


def fetch_web(
    url: str,
    *,
    title: str | None = None,
    opener: Callable[..., Any] | None = None,
    timeout: float = 10,
    max_bytes: int = 2_000_000,
) -> ImportResult:
    """Fetch a bounded web resource and attach a canonical body hash."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ImportAdapterError(
            "invalid_source",
            "web URL must use http or https",
            stage="fetch",
            retryable=False,
        )
    if parsed.username or parsed.password:
        raise ImportAdapterError(
            "invalid_source",
            "web URL credentials are not allowed",
            stage="fetch",
            retryable=False,
        )
    if max_bytes <= 0 or timeout <= 0:
        raise ValueError("web fetch limits must be positive")

    request = Request(url, headers={"User-Agent": "Zhiheng/0.2"})
    open_fn = opener or urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            media_type = response.headers.get_content_type()
            body = response.read(max_bytes + 1)
    except HTTPError as exc:
        retryable = exc.code == 429 or exc.code >= 500
        raise ImportAdapterError(
            "web_http_error",
            f"web server returned HTTP {exc.code}",
            stage="fetch",
            retryable=retryable,
            diagnostic_id=f"http-{exc.code}",
        ) from exc
    except TimeoutError as exc:
        raise ImportAdapterError(
            "web_timeout",
            "web request timed out",
            stage="fetch",
            retryable=True,
            diagnostic_id="web-timeout",
        ) from exc
    except URLError as exc:
        raise ImportAdapterError(
            "web_network_error",
            "web request failed",
            stage="fetch",
            retryable=True,
            diagnostic_id="web-network",
        ) from exc
    except OSError as exc:
        raise ImportAdapterError(
            "web_network_error",
            "web request failed",
            stage="fetch",
            retryable=True,
            diagnostic_id="web-network",
        ) from exc

    if len(body) > max_bytes:
        raise ImportAdapterError(
            "web_too_large",
            f"web response exceeds {max_bytes} bytes",
            stage="fetch",
            retryable=False,
            diagnostic_id="web-body-limit",
        )
    if media_type not in {"text/html", "text/plain"}:
        raise ImportAdapterError(
            "unsupported_media_type",
            f"web media type {media_type!r} is unsupported",
            stage="fetch",
            retryable=False,
            diagnostic_id=f"web-media-{media_type}",
        )

    decoded = body.decode("utf-8", errors="replace")
    normalized = normalize_web_text(decoded)
    return ImportResult(
        title or url,
        normalized,
        media_type,
        {
            "url": url,
            "fetched_at": datetime.now(UTC).isoformat(),
            "normalized_content_sha256": web_content_sha256(normalized),
            "response_media_type": media_type,
            "byte_size": str(len(body)),
        },
    )


def parse_ocr(
    data: bytes,
    *,
    title: str,
    provider: OCRProvider | None = None,
) -> ImportResult:
    """Run an injected OCR provider; callers should invoke this from a job."""

    if not data:
        raise ImportAdapterError(
            "parse_failed",
            "image body cannot be empty",
            stage="ocr",
            retryable=False,
        )
    extraction = (provider or UnavailableOCRProvider()).extract(data, title=title)
    if not extraction.text.strip():
        raise ImportAdapterError(
            "ocr_empty",
            "OCR provider returned no text",
            stage="ocr",
            retryable=False,
        )
    average_confidence = _average_confidence(extraction.blocks)
    metadata: dict[str, Any] = {
        "format": "ocr",
        "ocr_provider": extraction.provider,
        "ocr_language": extraction.language or "und",
        "ocr_requires_confirmation": average_confidence is not None and average_confidence < 0.8,
    }
    if average_confidence is not None:
        metadata["ocr_confidence"] = str(average_confidence)
    if extraction.blocks:
        metadata["ocr_blocks_json"] = json.dumps(
            [
                {
                    "text": block.text,
                    "confidence": block.confidence,
                    "page_no": block.page_no,
                    "bbox": block.bbox,
                }
                for block in extraction.blocks
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
    return ImportResult(title, extraction.text, "text/plain", metadata)


def normalize_web_text(value: str) -> str:
    """Normalize HTML/text for stable change detection and indexing."""

    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    text_value = parser.text if parser.seen_tag else value
    text_value = html.unescape(text_value)
    return re.sub(r"\s+", " ", text_value).strip()


def web_content_sha256(value: str) -> str:
    return hashlib.sha256(normalize_web_text(value).encode("utf-8")).hexdigest()


def web_refresh_state(
    previous_hash: str | None,
    fetched: ImportResult,
) -> Literal["new", "unchanged", "changed"]:
    """Classify a fetched page for refresh history/version handling."""

    current_hash = str(
        fetched.source_metadata.get("normalized_content_sha256") or fetched.content_sha256
    )
    if not previous_hash:
        return "new"
    return "unchanged" if previous_hash == current_hash else "changed"


class _VisibleTextParser(HTMLParser):
    _ignored = frozenset({"script", "style", "noscript", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0
        self.seen_tag = False

    @property
    def text(self) -> str:
        return " ".join(self.parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self.seen_tag = True
        if tag in self._ignored:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._ignored and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def _average_confidence(blocks: tuple[OCRTextBlock, ...]) -> float | None:
    values = [block.confidence for block in blocks if block.confidence is not None]
    return sum(values) / len(values) if values else None


__all__ = [
    "ImportAdapterError",
    "ImportResult",
    "OCRExtraction",
    "OCRProvider",
    "OCRTextBlock",
    "UnavailableOCRProvider",
    "fetch_web",
    "normalize_web_text",
    "parse_markdown",
    "parse_ocr",
    "parse_pdf",
    "web_content_sha256",
    "web_refresh_state",
]
