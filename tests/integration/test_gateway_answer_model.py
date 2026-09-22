from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_auth_and_model_gateway import (
    SystemExitTransport,
    _insert_provider,
    _migrated_session_factory,
)
from zhiheng.core.ids import sha256_text
from zhiheng.db.session import session_scope
from zhiheng.memory.context import MemoryContextEntry, MemoryContextSnapshot
from zhiheng.models import ModelGateway
from zhiheng.models._transports import TransportResponse, TransportRoute, _ApprovedOutboundPayload
from zhiheng.privacy.gateway import DeterministicPatternAnalyzer, PiiFinding, PrivacyPipeline
from zhiheng.query.contracts import PersonalizationRef
from zhiheng.query.gateway_model import GatewayAnswerModel, _wire_citation_id, _wire_memory_ref
from zhiheng.retrieval import RetrievalAuthorizer
from zhiheng.retrieval.citations import CitationBuilder
from zhiheng.retrieval.contracts import AuthorizedChunk, Citation, RetrievalSource


@dataclass
class CountingTransport:
    responses: list[str]
    calls: list[str]

    def complete(
        self,
        *,
        route: TransportRoute,
        payload: _ApprovedOutboundPayload,
    ) -> TransportResponse:
        del route
        self.calls.append(payload.text)
        return TransportResponse(
            text=self.responses.pop(0),
            response_hash=sha256_text(payload.text),
        )


@pytest.mark.parametrize("memory_id", ["memory-1", "aaaaaaaa-bbbb-4ccc-8ddd-138001380000"])
@pytest.mark.parametrize("local", [False, True])
def test_gateway_answer_model_uses_privacy_pipeline_and_strict_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_id: str,
    local: bool,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    chunk = _chunk(text="联系人 owner@example.test 的论文证据。")
    manifest = RetrievalAuthorizer().seal_manifest(
        query_hash=sha256_text("总结 owner@example.test 的论文"),
        chunks=[chunk],
    )
    citation = _citation_for(chunk)
    memory_context = _memory_context(sensitivity_level="sensitive" if local else "private")
    memory_context = replace(memory_context, entries=(replace(
        memory_context.entries[0], formal_memory_id=memory_id,
    ),))
    if memory_id != "memory-1":
        assert DeterministicPatternAnalyzer().analyze(memory_id)
    expected_ref = PersonalizationRef(
        formal_memory_id=memory_id,
        formal_version_id="memory-version-1",
        confirmation_generation=3,
        state_key="goal.research",
    )
    transport = CountingTransport(
        responses=[
            json.dumps(
                {
                    "answer": "基于证据回答，并按用户目标调整表达。",
                    "claims": [
                        {"text": "论文证据可用", "citation_ids": [_wire_citation_id(citation)]},
                    ],
                    "conflicts": [],
                    "assumptions": ["仅使用授权证据"],
                    "insufficiencies": [],
                    "output_tokens": 12,
                    "personalization_refs": [_wire_memory_ref(expected_ref)],
                },
                ensure_ascii=False,
            )
        ],
        calls=[],
    )
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(update={"external_models_enabled": not local}),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"ollama" if local else "openai-compatible": transport},
    )
    with session_scope(session_factory) as session:
        if local:
            _insert_provider(
                session, provider_kind="ollama", endpoint_url="http://127.0.0.1:11434",
                endpoint_origin="http://127.0.0.1:11434", secret_ref=None,
            )
        else:
            _insert_provider(session)

    result = GatewayAnswerModel(
        gateway=gateway,
        provider_id="provider-openai",
        model_id="gpt-test",
    ).generate_answer(
        query="总结 owner@example.test 的论文",
        manifest=manifest,
        citations=(citation,),
        max_output_tokens=20,
        memory_context=memory_context,
        conversation_context=({"query": "前一问", "answer": "当前会话前文"},),
    )

    assert result.answer == "基于证据回答，并按用户目标调整表达。"
    assert result.claims[0].citation_ids == (citation.citation_id,)
    assert result.personalization_refs == (expected_ref,)
    payload = json.loads(transport.calls[0])
    assert payload["prompt_version"] == "gateway-answer-model-v3"
    assert payload["user_query"] == "总结 [REDACTED_EMAIL_ADDRESS] 的论文"
    assert payload["KNOWLEDGE_EVIDENCE"][0]["text"] == (
        "联系人 [REDACTED_EMAIL_ADDRESS] 的论文证据。"
    )
    assert payload["KNOWLEDGE_EVIDENCE"][0]["citation_ids"] == [_wire_citation_id(citation)]
    assert payload["USER_CONFIRMED_CONTEXT"]["entries"][0]["value"] == {
        "goal": "prefer concise answers"
    }
    assert payload["CONVERSATION_CONTEXT"] == [{"query": "前一问", "answer": "当前会话前文"}]
    provenance = payload["USER_CONFIRMED_CONTEXT"]["entries"][0]["provenance"]
    assert provenance["memory_ref_id"] == _wire_memory_ref(expected_ref)
    assert "formal_memory_id" not in provenance
    assert memory_id not in transport.calls[0]


def test_gateway_answer_model_rejects_unscoped_json_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    chunk = _chunk()
    manifest = RetrievalAuthorizer().seal_manifest(query_hash=sha256_text("query"), chunks=[chunk])
    transport = CountingTransport(
        responses=[
            json.dumps(
                {
                    "answer": "bad",
                    "claims": [{"text": "bad", "citation_ids": ["unknown-citation"]}],
                    "conflicts": [],
                    "assumptions": [],
                    "insufficiencies": [],
                    "output_tokens": 1,
                    "personalization_refs": [],
                }
            )
        ],
        calls=[],
    )
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(update={"external_models_enabled": True}),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": transport},
    )
    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(ValueError, match="citation outside authorized scope"):
        GatewayAnswerModel(
            gateway=gateway,
            provider_id="provider-openai",
            model_id="gpt-test",
        ).generate_answer(
            query="query",
            manifest=manifest,
            citations=(_citation_for(chunk),),
            max_output_tokens=20,
        )

    assert len(transport.calls) == 1


def test_gateway_answer_model_requires_local_for_sensitive_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    monkeypatch.setenv("ZHIHENG_PRIVATE_TEST_SECRET", "secret-value")
    chunk = _chunk()
    manifest = RetrievalAuthorizer().seal_manifest(query_hash=sha256_text("query"), chunks=[chunk])
    transport = CountingTransport(responses=[], calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(update={"external_models_enabled": True}),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": transport},
    )
    with session_scope(session_factory) as session:
        _insert_provider(session)

    with pytest.raises(PermissionError, match="local model"):
        GatewayAnswerModel(
            gateway=gateway,
            provider_id="provider-openai",
            model_id="gpt-test",
        ).generate_answer(
            query="query",
            manifest=manifest,
            citations=(_citation_for(chunk),),
            memory_context=_memory_context(sensitivity_level="sensitive"),
        )

    assert transport.calls == []


def test_gateway_answer_model_rejects_unknown_memory_sensitivity_before_gateway(
    tmp_path: Path,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    chunk = _chunk()
    manifest = RetrievalAuthorizer().seal_manifest(query_hash=sha256_text("query"), chunks=[chunk])
    transport = CountingTransport(responses=[], calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings,
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": transport},
    )

    with pytest.raises(PermissionError, match="unknown memory sensitivity"):
        GatewayAnswerModel(
            gateway=gateway,
            provider_id="provider-openai",
            model_id="gpt-test",
        ).generate_answer(
            query="query",
            manifest=manifest,
            citations=(_citation_for(chunk),),
            memory_context=_memory_context(sensitivity_level="mystery"),
        )

    assert transport.calls == []


def test_retrieval_retry_keeps_unresolved_dispatch_binding(tmp_path: Path) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    crashing = SystemExitTransport()
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(update={"external_models_enabled": True}),
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": crashing},
    )
    with session_scope(session_factory) as session:
        _insert_provider(session)
    model = GatewayAnswerModel(gateway=gateway, provider_id="provider-openai", model_id="gpt-test")
    chunk = _chunk()
    authorizer = RetrievalAuthorizer()
    manifests = [authorizer.seal_manifest(query_hash=sha256_text("query"), chunks=[chunk])
                 for _ in range(2)]
    citations = [CitationBuilder().build(manifest, chunk_id=chunk.chunk_id,
                                        start_offset=0, end_offset=len(chunk.text))
                 for manifest in manifests]
    assert manifests[0].manifest_id != manifests[1].manifest_id
    assert citations[0].citation_id != citations[1].citation_id
    with pytest.raises(SystemExit):
        model.generate_answer(query="query", manifest=manifests[0], citations=[citations[0]])
    with pytest.raises(PermissionError, match="manual reconcile"):
        model.generate_answer(query="query", manifest=manifests[1], citations=[citations[1]])
    assert crashing.calls == 1


def test_wire_reference_binds_evidence_version_and_span() -> None:
    original = _citation_for(_chunk())
    alias = _wire_citation_id(original)
    assert alias == _wire_citation_id(replace(original, citation_id="new-retrieval-id"))
    for changed in (
        replace(original, source_version_id="next-version"),
        replace(original, source_id="other-source"),
        replace(original, evidence_object_id="other-object"),
        replace(original, offset=(0, 2)),
        replace(original, quote_hash=sha256_text("different evidence")),
    ):
        assert alias != _wire_citation_id(changed)


@pytest.mark.parametrize("field", [
    "source_id", "source_version_id", "evidence_object_id", "quote_hash",
])
def test_gateway_answer_model_rejects_mismatched_citation_before_dispatch(
    tmp_path: Path, field: str,
) -> None:
    _, settings, session_factory = _migrated_session_factory(tmp_path)
    transport = CountingTransport(responses=[], calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory, settings=settings,
        privacy_pipeline=PrivacyPipeline(analyzer=DeterministicPatternAnalyzer()),
        transports={"openai-compatible": transport},
    )
    chunk = _chunk()
    manifest = RetrievalAuthorizer().seal_manifest(query_hash=sha256_text("query"), chunks=[chunk])
    changes: dict[str, Any] = {field: "different-source-value"}
    citation = replace(_citation_for(chunk), **changes)
    model = GatewayAnswerModel(gateway=gateway, provider_id="not-configured", model_id="gpt-test")
    with pytest.raises(ValueError, match="does not match authorized manifest"):
        model.generate_answer(query="query", manifest=manifest, citations=[citation])
    assert transport.calls == []


def test_gateway_answer_model_refuses_unavailable_classification(tmp_path: Path) -> None:
    class UnavailableAnalyzer:
        engine_name = "unavailable-test-analyzer"

        def analyze(self, text: str) -> list[PiiFinding]:
            raise RuntimeError("synthetic unavailable classifier")

    _, settings, session_factory = _migrated_session_factory(tmp_path)
    transport = CountingTransport(responses=[], calls=[])
    gateway = ModelGateway._for_test(
        session_factory=session_factory,
        settings=settings.model_copy(update={"external_models_enabled": True}),
        privacy_pipeline=PrivacyPipeline(analyzer=UnavailableAnalyzer()),
        transports={"openai-compatible": transport},
    )
    with session_scope(session_factory) as session:
        _insert_provider(session)
    chunk = _chunk()
    manifest = RetrievalAuthorizer().seal_manifest(query_hash=sha256_text("query"), chunks=[chunk])
    with pytest.raises(PermissionError):
        model = GatewayAnswerModel(
            gateway=gateway, provider_id="provider-openai", model_id="gpt-test",
        )
        model.generate_answer(
            query="query", manifest=manifest, citations=[_citation_for(chunk)],
        )
    assert transport.calls == []


def _chunk(text: str = "正式知识库证据。") -> AuthorizedChunk:
    return AuthorizedChunk(
        source_type="knowledge_object",
        source_id="knowledge-1",
        source_version_id="version-1",
        chunk_id="chunk-1",
        confirmation_generation=1,
        generation=None,
        title="正式证据",
        text=text,
        span_start=0,
        span_end=len(text),
        content_version_id="content-1",
        content_span_id="span-1",
        evidence_object_id="evidence-1",
        page_no=None,
        section_path=None,
        quote_hash=sha256_text(text),
        score=1.0,
        rank=1,
        retrievers=(RetrievalSource.LEXICAL,),
    )


def _citation_for(chunk: AuthorizedChunk) -> Citation:
    return Citation(
        citation_id="citation-1",
        source_type=chunk.source_type,
        source_id=chunk.source_id,
        source_version_id=chunk.source_version_id,
        chunk_id=chunk.chunk_id,
        evidence_object_id=chunk.evidence_object_id,
        content_version_id=chunk.content_version_id,
        content_span_id=chunk.content_span_id,
        content_span=(chunk.span_start, chunk.span_end),
        offset=(chunk.span_start, chunk.span_end),
        page_no=chunk.page_no,
        section_path=chunk.section_path,
        quote_hash=chunk.quote_hash or "",
    )


def _memory_context(*, sensitivity_level: str) -> MemoryContextSnapshot:
    entry = MemoryContextEntry(
        formal_memory_id="memory-1",
        formal_version_id="memory-version-1",
        confirmation_generation=3,
        state_key="goal.research",
        layer="L0",
        value_json=json.dumps({"goal": "prefer concise answers"}, sort_keys=True),
        memory_type="goal",
        origin_kind="explicit",
        source_kind="user_confirmed",
        sensitivity_level=sensitivity_level,
        confidence=1.0,
        valid_from="2026-01-01T00:00:00Z",
        valid_to=None,
    )
    payload = {
        "entries": [entry.canonical_payload()],
        "limit_policy_version": "memory-context-v1",
        "query_hash": sha256_text("query"),
    }
    return MemoryContextSnapshot(
        query_hash=sha256_text("query"),
        topic_prefix=None,
        entries=(entry,),
        digest=sha256_text(json.dumps(payload, sort_keys=True)),
        truncated=False,
        omitted_count=0,
        eligible_count=1,
        source_digest="source-digest",
    )
