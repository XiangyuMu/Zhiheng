from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from zhiheng.core.ids import sha256_text

Decision = Literal["approved", "refused"]
ClassificationStatus = Literal["clear", "uncertain", "sensitive"]
RedactionStatus = Literal["not_needed", "complete", "uncertain"]
PrivacyPipelineStatus = Literal["clear", "redacted", "refused"]

_EMAIL_PATTERN = r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
_PHONE_PATTERN = (
    r"(?:(?:\+?86[-\s]?)?1[3-9]\d{9})"
    r"|(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}"
)
_CN_ID_PATTERN = (
    r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"
)
_SECRET_PATTERN = (
    r"(?i)(?:\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*[A-Za-z0-9_\-]{8,}"
    r"|\bsk-[A-Za-z0-9_\-]{16,}\b)"
)
_CARD_LIKE_PATTERN = r"\b(?:\d[ -]?){13,19}\b"
_PERSON_PATTERN = (
    r"(?:姓名|联系人|患者|病人|person|patient)\s*[:：]\s*"
    r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z.\s]{1,40}"
)
_ADDRESS_PATTERN = (
    r"(?:住址|地址|家庭地址|收货地址|address)\s*[:：]\s*"
    r"[\u4e00-\u9fffA-Za-z0-9#号楼室单元弄路街道巷,\-.\s]{4,120}"
)
_MEDICAL_PATTERN = (
    r"(?:诊断|病历|病史|处方|用药|检查报告|medical|diagnosis|prescription)"
    r"\s*[:：]\s*[\u4e00-\u9fffA-Za-z0-9,\-.\s]{2,120}"
)
_FIRST_PERSON_PATTERN = re.compile(r"(?i)(?:\bI\b|\bme\b|\bmy\b|\bmine\b|我|我的|本人|自己)")
_SENSITIVE_CONTEXT_PATTERN = re.compile(
    r"(?i)(health|diagnosis|disease|therapy|finance|salary|debt|address|"
    r"relationship|medical|bank|住址|地址|健康|疾病|诊断|治疗|财务|收入|工资|"
    r"负债|银行|感情|关系|伴侣)"
)


@dataclass(frozen=True)
class PiiFinding:
    entity_type: str
    start: int
    end: int
    score: float


class PiiAnalyzerPort(Protocol):
    @property
    def engine_name(self) -> str: ...

    def analyze(self, text: str) -> list[PiiFinding]: ...


class PiiAnonymizerPort(Protocol):
    @property
    def engine_name(self) -> str: ...

    def anonymize(self, text: str, findings: list[PiiFinding]) -> str: ...


@dataclass(frozen=True)
class PrivacyPipelineResult:
    status: PrivacyPipelineStatus
    final_payload: str
    final_payload_hash: str
    minimized_payload_hash: str
    classification_payload_hash: str
    redaction_payload_hash: str
    recheck_payload_hash: str
    classification_status: ClassificationStatus
    redaction_status: RedactionStatus
    analyzer_name: str
    anonymizer_name: str
    finding_types: tuple[str, ...]
    reason: str

    @property
    def approved_for_network(self) -> bool:
        return self.status in {"clear", "redacted"}


@dataclass(frozen=True)
class OutboundPayloadRequest:
    provider_id: str | None
    payload_hash: str
    classification_status: ClassificationStatus
    redaction_status: RedactionStatus


@dataclass(frozen=True)
class OutboundPayloadDecision:
    decision: Decision
    reason: str


class DeterministicPatternAnalyzer:
    engine_name = "deterministic-patterns-v1"

    def __init__(self) -> None:
        self._patterns = (
            ("EMAIL_ADDRESS", re.compile(_EMAIL_PATTERN)),
            ("PHONE_NUMBER", re.compile(_PHONE_PATTERN)),
            ("CN_NATIONAL_ID", re.compile(_CN_ID_PATTERN)),
            ("SECRET", re.compile(_SECRET_PATTERN)),
            ("CARD_LIKE_NUMBER", re.compile(_CARD_LIKE_PATTERN)),
            ("PERSON", re.compile(_PERSON_PATTERN, re.IGNORECASE)),
            ("ADDRESS", re.compile(_ADDRESS_PATTERN, re.IGNORECASE)),
            ("MEDICAL", re.compile(_MEDICAL_PATTERN, re.IGNORECASE)),
        )

    def analyze(self, text: str) -> list[PiiFinding]:
        findings: list[PiiFinding] = []
        for entity_type, pattern in self._patterns:
            for match in pattern.finditer(text):
                if entity_type == "CARD_LIKE_NUMBER" and not _is_card_like(match.group(0)):
                    continue
                findings.append(
                    PiiFinding(
                        entity_type=entity_type,
                        start=match.start(),
                        end=match.end(),
                        score=0.95,
                    )
                )
        return _deduplicate_findings(findings)


class PresidioPatternAnalyzer:
    engine_name = "presidio-noop-patterns-v1"

    def __init__(self) -> None:
        analyzer_module: Any = importlib.import_module("presidio_analyzer")
        nlp_module: Any = importlib.import_module("presidio_analyzer.nlp_engine")
        recognizers = []
        for entity_type, pattern in (
            ("EMAIL_ADDRESS", _EMAIL_PATTERN),
            ("PHONE_NUMBER", _PHONE_PATTERN),
            ("CN_NATIONAL_ID", _CN_ID_PATTERN),
            ("SECRET", _SECRET_PATTERN),
            ("CARD_LIKE_NUMBER", _CARD_LIKE_PATTERN),
            ("PERSON", _PERSON_PATTERN),
            ("ADDRESS", _ADDRESS_PATTERN),
            ("MEDICAL", _MEDICAL_PATTERN),
        ):
            for language in ("en", "zh"):
                recognizers.append(
                    analyzer_module.PatternRecognizer(
                        supported_entity=entity_type,
                        patterns=[analyzer_module.Pattern(entity_type, pattern, 0.95)],
                        supported_language=language,
                    )
                )
        registry = analyzer_module.RecognizerRegistry(
            recognizers=recognizers,
            supported_languages=["en", "zh"],
        )
        analyzer_engine = analyzer_module.AnalyzerEngine(
            registry=registry,
            nlp_engine=nlp_module.NoOpNlpEngine(
                models=[
                    {"lang_code": "en", "model_name": "noop"},
                    {"lang_code": "zh", "model_name": "noop"},
                ]
            ),
            supported_languages=["en", "zh"],
        )
        self._analyzer_engine = analyzer_engine

    def analyze(self, text: str) -> list[PiiFinding]:
        language = "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"
        results = self._analyzer_engine.analyze(text=text, language=language)
        return _deduplicate_findings(
            [
                PiiFinding(
                    entity_type=str(result.entity_type),
                    start=int(result.start),
                    end=int(result.end),
                    score=float(result.score),
                )
                for result in results
            ]
        )


class DeterministicReplacer:
    engine_name = "deterministic-replacer-v1"

    def anonymize(self, text: str, findings: list[PiiFinding]) -> str:
        if not findings:
            return text
        redacted = text
        for finding in sorted(findings, key=lambda item: item.start, reverse=True):
            redacted = (
                redacted[: finding.start]
                + f"[REDACTED_{finding.entity_type}]"
                + redacted[finding.end :]
            )
        return redacted


@dataclass(frozen=True)
class _UnavailableAnalyzer:
    reason: str
    engine_name: str = "presidio-analyzer-unavailable"

    def analyze(self, text: str) -> list[PiiFinding]:
        raise RuntimeError(self.reason)


class PrivacyPipeline:
    def __init__(
        self,
        *,
        analyzer: PiiAnalyzerPort | None = None,
        anonymizer: PiiAnonymizerPort | None = None,
        max_payload_chars: int = 12000,
    ) -> None:
        self._analyzer = analyzer or _default_analyzer()
        self._anonymizer = anonymizer or _default_anonymizer()
        self._max_payload_chars = max_payload_chars

    def prepare_for_model(self, payload: str) -> PrivacyPipelineResult:
        minimized = _minimize_payload(payload)
        if not minimized:
            return self._refused("payload is empty after minimization")
        if len(minimized) > self._max_payload_chars:
            return self._refused("payload exceeds privacy gateway size limit")
        if _contains_sensitive_personal_semantics(minimized):
            return self._refused("first-person sensitive personal context requires local handling")

        try:
            findings = self._analyzer.analyze(minimized)
        except Exception:
            return self._refused("privacy analyzer is unavailable")
        finding_types = tuple(sorted({finding.entity_type for finding in findings}))
        if not findings:
            return PrivacyPipelineResult(
                status="clear",
                final_payload=minimized,
                final_payload_hash=sha256_text(minimized),
                minimized_payload_hash=sha256_text(minimized),
                classification_payload_hash=sha256_text(minimized),
                redaction_payload_hash=sha256_text(minimized),
                recheck_payload_hash=sha256_text(minimized),
                classification_status="clear",
                redaction_status="not_needed",
                analyzer_name=self._analyzer.engine_name,
                anonymizer_name=self._anonymizer.engine_name,
                finding_types=finding_types,
                reason="payload contains no configured sensitive patterns",
            )

        try:
            redacted = self._anonymizer.anonymize(minimized, findings)
        except Exception:
            return self._refused("privacy anonymizer is unavailable")
        if redacted == minimized:
            return self._refused("payload redaction made no change")
        try:
            residual_findings = self._analyzer.analyze(redacted)
        except Exception:
            return self._refused("privacy analyzer is unavailable")
        if residual_findings:
            return PrivacyPipelineResult(
                status="refused",
                final_payload="",
                final_payload_hash=sha256_text(""),
                minimized_payload_hash=sha256_text(minimized),
                classification_payload_hash=sha256_text(minimized),
                redaction_payload_hash=sha256_text(redacted),
                recheck_payload_hash=sha256_text(redacted),
                classification_status="sensitive",
                redaction_status="uncertain",
                analyzer_name=self._analyzer.engine_name,
                anonymizer_name=self._anonymizer.engine_name,
                finding_types=finding_types,
                reason="redaction recheck found residual sensitive patterns",
            )
        return PrivacyPipelineResult(
            status="redacted",
            final_payload=redacted,
            final_payload_hash=sha256_text(redacted),
            minimized_payload_hash=sha256_text(minimized),
            classification_payload_hash=sha256_text(minimized),
            redaction_payload_hash=sha256_text(redacted),
            recheck_payload_hash=sha256_text(redacted),
            classification_status="clear",
            redaction_status="complete",
            analyzer_name=self._analyzer.engine_name,
            anonymizer_name=self._anonymizer.engine_name,
            finding_types=finding_types,
            reason="payload was redacted and rechecked",
        )

    def _refused(self, reason: str) -> PrivacyPipelineResult:
        return PrivacyPipelineResult(
            status="refused",
            final_payload="",
            final_payload_hash=sha256_text(""),
            minimized_payload_hash=sha256_text(""),
            classification_payload_hash=sha256_text(""),
            redaction_payload_hash=sha256_text(""),
            recheck_payload_hash=sha256_text(""),
            classification_status="uncertain",
            redaction_status="uncertain",
            analyzer_name=self._analyzer.engine_name,
            anonymizer_name=self._anonymizer.engine_name,
            finding_types=(),
            reason=reason,
        )


def authorize_outbound_payload(
    request: OutboundPayloadRequest,
    external_models_enabled: bool,
) -> OutboundPayloadDecision:
    if not external_models_enabled:
        return OutboundPayloadDecision("refused", "external models are disabled")
    if request.provider_id is None:
        return OutboundPayloadDecision("refused", "no approved provider configured")
    if len(request.payload_hash) != 64:
        return OutboundPayloadDecision("refused", "payload hash is invalid")
    if request.classification_status != "clear":
        return OutboundPayloadDecision("refused", "payload classification is not clear")
    if request.redaction_status not in {"not_needed", "complete"}:
        return OutboundPayloadDecision("refused", "payload redaction could not be verified")
    return OutboundPayloadDecision("approved", "payload passed configured privacy gateway checks")


def _default_analyzer() -> PiiAnalyzerPort:
    try:
        return PresidioPatternAnalyzer()
    except Exception as exc:
        return _UnavailableAnalyzer(str(exc))


def _default_anonymizer() -> PiiAnonymizerPort:
    return DeterministicReplacer()


def _minimize_payload(payload: str) -> str:
    return re.sub(r"\s+", " ", payload).strip()


def _deduplicate_findings(findings: list[PiiFinding]) -> list[PiiFinding]:
    unique: dict[tuple[int, int, str], PiiFinding] = {}
    for finding in findings:
        if finding.start < finding.end:
            unique[(finding.start, finding.end, finding.entity_type)] = finding
    selected: list[PiiFinding] = []
    for finding in sorted(
        unique.values(),
        key=lambda item: (item.start, -(item.end - item.start), item.entity_type),
    ):
        overlaps = [
            existing
            for existing in selected
            if finding.start < existing.end and existing.start < finding.end
        ]
        if not overlaps:
            selected.append(finding)
            continue
        longest_overlap = max(overlaps, key=lambda item: item.end - item.start)
        if finding.end - finding.start > longest_overlap.end - longest_overlap.start:
            selected = [
                existing
                for existing in selected
                if not (finding.start < existing.end and existing.start < finding.end)
            ]
            selected.append(finding)
    return sorted(selected, key=lambda item: (item.start, item.end, item.entity_type))


def _contains_sensitive_personal_semantics(text: str) -> bool:
    return bool(_FIRST_PERSON_PATTERN.search(text) and _SENSITIVE_CONTEXT_PATTERN.search(text))


def _is_card_like(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 13 <= len(digits) <= 19 and len(set(digits)) > 1
