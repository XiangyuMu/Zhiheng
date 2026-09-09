from __future__ import annotations

from zhiheng.core.ids import sha256_json, sha256_text
from zhiheng.retrieval.contracts import AuthorizedContextManifest, Citation


class CitationBuilder:
    def build(
        self,
        manifest: AuthorizedContextManifest,
        *,
        chunk_id: str,
        start_offset: int,
        end_offset: int,
    ) -> Citation:
        chunk = next((item for item in manifest.chunks if item.chunk_id == chunk_id), None)
        if chunk is None:
            raise ValueError("unknown chunk_id for sealed manifest")
        if (
            start_offset < chunk.span_start
            or end_offset > chunk.span_end
            or end_offset < start_offset
        ):
            raise ValueError("citation span is outside authorized context")
        if len(chunk.text) != chunk.span_end - chunk.span_start:
            raise ValueError("authorized context span does not match text length")

        relative_start = start_offset - chunk.span_start
        relative_end = end_offset - chunk.span_start
        quote = chunk.text[relative_start:relative_end]
        quote_hash = sha256_text(quote)
        if chunk.quote_hash is not None and chunk.quote_hash != sha256_text(chunk.text):
            raise ValueError("authorized context quote hash is stale")

        citation_id = sha256_json(
            {
                "manifest_id": manifest.manifest_id,
                "seal": manifest.seal,
                "chunk_id": chunk.chunk_id,
                "start_offset": start_offset,
                "end_offset": end_offset,
                "quote_hash": quote_hash,
            }
        )
        return Citation(
            citation_id=citation_id,
            source_type=chunk.source_type,
            source_id=chunk.source_id,
            source_version_id=chunk.source_version_id,
            chunk_id=chunk.chunk_id,
            evidence_object_id=chunk.evidence_object_id,
            content_version_id=chunk.content_version_id,
            content_span_id=chunk.content_span_id,
            content_span=(chunk.span_start, chunk.span_end),
            offset=(start_offset, end_offset),
            page_no=chunk.page_no,
            section_path=chunk.section_path,
            quote_hash=quote_hash,
        )
