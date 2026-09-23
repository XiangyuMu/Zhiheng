import pytest

from zhiheng.knowledge.import_adapters import (
    ImportAdapterError,
    OCRExtraction,
    OCRTextBlock,
    fetch_web,
    normalize_web_text,
    parse_markdown,
    parse_ocr,
    parse_pdf,
    web_content_sha256,
    web_refresh_state,
)


def test_markdown_adapter_preserves_text() -> None:
    result = parse_markdown(b"# title\nbody", title="doc")
    assert result.text.startswith("# title")
    assert result.media_type == "text/markdown"


def test_pdf_adapter_has_explicit_failure_semantics() -> None:
    with pytest.raises(ValueError, match="parse_failed"):
        parse_pdf(b"not-pdf", title="doc")
    with pytest.raises(ValueError, match="parse_failed"):
        parse_pdf(b"%PDF-1.7", title="doc")


def test_ocr_adapter_reports_retryable_provider_failure() -> None:
    with pytest.raises(ImportAdapterError, match="unsupported") as exc_info:
        parse_ocr(b"image", title="scan")
    assert exc_info.value.code == "ocr_provider_unavailable"
    assert exc_info.value.stage == "ocr"
    assert exc_info.value.retryable is True


def test_ocr_provider_result_preserves_metadata_and_low_confidence() -> None:
    class Provider:
        def extract(self, data: bytes, *, title: str) -> OCRExtraction:
            assert data == b"image"
            assert title == "scan"
            return OCRExtraction(
                text="扫描文本",
                language="zh",
                provider="test-ocr",
                blocks=(OCRTextBlock("扫描文本", confidence=0.5, page_no=1),),
            )

    result = parse_ocr(b"image", title="scan", provider=Provider())
    assert result.text == "扫描文本"
    assert result.media_type == "text/plain"
    assert result.source_metadata["ocr_provider"] == "test-ocr"
    assert result.source_metadata["ocr_language"] == "zh"
    assert result.source_metadata["ocr_requires_confirmation"] is True
    assert result.source_metadata["ocr_blocks_json"]


class _Response:
    def __init__(self, body: bytes, media_type: str = "text/html") -> None:
        from email.message import Message

        self.headers = Message()
        self.headers["Content-Type"] = media_type
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def test_web_fetch_normalizes_visible_text_and_hashes_content() -> None:
    fetched = fetch_web(
        "https://example.test/article",
        opener=lambda request, timeout: _Response(
            b"<html><script>ignore()</script><h1> Title </h1><p>Hello&nbsp; world</p></html>"
        ),
    )
    assert fetched.text == "Title Hello world"
    assert fetched.source_metadata["url"] == "https://example.test/article"
    assert fetched.source_metadata["normalized_content_sha256"] == web_content_sha256(
        "Title Hello world"
    )
    assert web_refresh_state(None, fetched) == "new"
    previous_hash = str(fetched.source_metadata["normalized_content_sha256"])
    assert web_refresh_state(previous_hash, fetched) == "unchanged"


def test_web_refresh_detects_only_visible_content_changes() -> None:
    old = web_content_sha256("<p>Hello</p><style>old</style>")
    same = web_content_sha256("<p> Hello </p><style>new</style>")
    changed = web_content_sha256("<p>Goodbye</p>")
    assert normalize_web_text("<p>Hello</p><style>old</style>") == "Hello"
    assert old == same
    assert old != changed


def test_web_fetch_returns_stable_failure_details() -> None:
    with pytest.raises(ImportAdapterError) as exc_info:
        fetch_web("file:///tmp/private")
    assert exc_info.value.code == "invalid_source"
    assert exc_info.value.retryable is False

    with pytest.raises(ImportAdapterError) as exc_info:
        fetch_web(
            "https://example.test/file",
            opener=lambda request, timeout: _Response(b"pdf", "application/pdf"),
        )
    assert exc_info.value.code == "unsupported_media_type"
    assert exc_info.value.stage == "fetch"
