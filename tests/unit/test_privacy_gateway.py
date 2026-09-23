from __future__ import annotations

import pytest

from zhiheng.privacy.gateway import (
    DeterministicPatternAnalyzer,
    OutboundPayloadRequest,
    PiiFinding,
    PrivacyPipeline,
    authorize_outbound_payload,
)


def test_privacy_gateway_refuses_external_payloads_by_default() -> None:
    decision = authorize_outbound_payload(
        OutboundPayloadRequest(
            provider_id="provider",
            payload_hash="0" * 64,
            classification_status="clear",
            redaction_status="complete",
        ),
        external_models_enabled=False,
    )

    assert decision.decision == "refused"
    assert "disabled" in decision.reason


def test_privacy_gateway_refuses_uncertain_classification() -> None:
    decision = authorize_outbound_payload(
        OutboundPayloadRequest(
            provider_id="provider",
            payload_hash="0" * 64,
            classification_status="uncertain",
            redaction_status="complete",
        ),
        external_models_enabled=True,
    )

    assert decision.decision == "refused"
    assert "classification" in decision.reason


def test_privacy_pipeline_redacts_email_phone_and_secret_before_network() -> None:
    pipeline = PrivacyPipeline(analyzer=DeterministicPatternAnalyzer())

    result = pipeline.prepare_for_model(
        "请总结 owner@example.test，电话 13800138000，身份证 110105199001011234，"
        "api_key=abcdef123456"
    )

    assert result.status == "redacted"
    assert "owner@example.test" not in result.final_payload
    assert "13800138000" not in result.final_payload
    assert "110105199001011234" not in result.final_payload
    assert "abcdef123456" not in result.final_payload
    assert result.classification_status == "clear"
    assert result.redaction_status == "complete"


def test_privacy_pipeline_redacts_card_like_person_address_and_medical_text() -> None:
    pipeline = PrivacyPipeline(analyzer=DeterministicPatternAnalyzer())

    result = pipeline.prepare_for_model(
        "姓名：张三，住址：北京市海淀区中关村1号，卡号 4111 1111 1111 1111，诊断：高血压。"
    )

    assert result.status == "redacted"
    assert "张三" not in result.final_payload
    assert "北京市海淀区中关村1号" not in result.final_payload
    assert "4111 1111 1111 1111" not in result.final_payload
    assert "高血压" not in result.final_payload
    assert "CARD_LIKE_NUMBER" in result.finding_types
    assert "PERSON" in result.finding_types
    assert "ADDRESS" in result.finding_types
    assert "MEDICAL" in result.finding_types


def test_privacy_pipeline_prefers_longest_overlapping_finding_to_avoid_partial_leak() -> None:
    pipeline = PrivacyPipeline(analyzer=DeterministicPatternAnalyzer())

    result = pipeline.prepare_for_model("address: 123 Main Street, Apt 5")

    assert result.status == "redacted"
    assert "123 Main Street" not in result.final_payload
    assert "Apt 5" not in result.final_payload


def test_privacy_pipeline_fails_closed_when_redaction_recheck_has_residue() -> None:
    class BrokenAnonymizer:
        engine_name = "broken"

        def anonymize(self, text: str, findings: list[PiiFinding]) -> str:
            return text.replace("owner@example.test", "leaked@example.test")

    pipeline = PrivacyPipeline(
        analyzer=DeterministicPatternAnalyzer(),
        anonymizer=BrokenAnonymizer(),
    )

    result = pipeline.prepare_for_model("email: owner@example.test")

    assert result.status == "refused"
    assert result.final_payload == ""
    assert result.redaction_status == "uncertain"


def test_privacy_pipeline_fails_closed_on_minimization_failure() -> None:
    pipeline = PrivacyPipeline(analyzer=DeterministicPatternAnalyzer())

    result = pipeline.prepare_for_model(" \n\t ")

    assert result.status == "refused"
    assert "empty" in result.reason


def test_privacy_pipeline_refuses_first_person_sensitive_semantics() -> None:
    pipeline = PrivacyPipeline(analyzer=DeterministicPatternAnalyzer())

    result = pipeline.prepare_for_model("我的财务和住址应该如何规划？")

    assert result.status == "refused"
    assert "first-person" in result.reason


def test_privacy_pipeline_fails_closed_when_default_presidio_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zhiheng.privacy.gateway as gateway

    class UnavailableAnalyzer:
        engine_name = "presidio-analyzer-unavailable"

        def analyze(self, text: str) -> list[PiiFinding]:
            raise RuntimeError("missing presidio")

    monkeypatch.setattr(gateway, "_default_analyzer", lambda: UnavailableAnalyzer())
    pipeline = PrivacyPipeline()

    result = pipeline.prepare_for_model("contact owner@example.test")

    assert result.status == "refused"
    assert result.final_payload == ""
    assert "analyzer" in result.reason


def test_authorize_outbound_payload_rejects_invalid_hash() -> None:
    decision = authorize_outbound_payload(
        OutboundPayloadRequest(
            provider_id="provider",
            payload_hash="short",
            classification_status="clear",
            redaction_status="complete",
        ),
        external_models_enabled=True,
    )

    assert decision.decision == "refused"
    assert "hash" in decision.reason


def test_presidio_analyzer_boundary_is_available_when_dependency_installed() -> None:
    pytest.importorskip("presidio_analyzer")
    pipeline = PrivacyPipeline()

    result = pipeline.prepare_for_model("contact owner@example.test")

    assert result.analyzer_name == "presidio-noop-patterns-v1"
    assert result.anonymizer_name == "deterministic-replacer-v1"
    assert "owner@example.test" not in result.final_payload
