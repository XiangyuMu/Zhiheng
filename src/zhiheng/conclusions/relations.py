"""Deterministic relation suggestions for versioned conclusion entries."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

_NEGATIONS = ("不", "不能", "无法", "不应", "不应该", "无需", "不是", "不会", "禁止")
_CONTRADICTION_PAIRS = (
    ("有效", "无效"),
    ("可行", "不可行"),
    ("适合", "不适合"),
    ("提高", "降低"),
    ("增加", "减少"),
    ("支持", "反对"),
    ("应该", "不应该"),
    ("需要", "不需要"),
    ("允许", "禁止"),
    ("成功", "失败"),
)
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-z0-9]+", re.IGNORECASE)


def normalize_claim(value: str) -> str:
    return re.sub(r"\s+", "", value).strip("。！？!?，,；;：:")


def _tokens(value: str) -> set[str]:
    normalized = normalize_claim(value).lower()
    return {
        token
        for token in _TOKEN_RE.findall(normalized)
        if token not in {"的", "了", "是", "可"}
    }


def _premises(payload: dict[str, Any]) -> set[str]:
    return {
        normalize_claim(str(item.get("text", ""))).lower()
        for item in payload.get("premises", [])
        if str(item.get("text", "")).strip()
    }


def _polarity(claim: str) -> bool:
    return any(marker in normalize_claim(claim) for marker in _NEGATIONS)


def _explicit_contradiction(left: str, right: str) -> bool:
    left_normalized, right_normalized = normalize_claim(left), normalize_claim(right)
    return any(
        (first in left_normalized and second in right_normalized)
        or (second in left_normalized and first in right_normalized)
        for first, second in _CONTRADICTION_PAIRS
    )


def relation_kind(new_payload: dict[str, Any], old_payload: dict[str, Any]) -> str | None:
    """Return the strongest explainable relation, or None when unrelated."""
    new_claim = str(new_payload.get("claim", ""))
    old_claim = str(old_payload.get("claim", ""))
    new_normalized = normalize_claim(new_claim).lower()
    old_normalized = normalize_claim(old_claim).lower()
    if not new_normalized or not old_normalized:
        return None
    new_premises, old_premises = _premises(new_payload), _premises(old_payload)
    premise_match = new_premises == old_premises
    if new_normalized == old_normalized:
        return "duplicate" if premise_match else "conditional_coexistence"

    new_tokens, old_tokens = _tokens(new_claim), _tokens(old_claim)
    if not new_tokens or not old_tokens:
        return None
    overlap = len(new_tokens & old_tokens) / max(1, min(len(new_tokens), len(old_tokens)))
    sequence = SequenceMatcher(None, new_normalized, old_normalized).ratio()
    if overlap < 0.45 and sequence < 0.48:
        return None
    if not premise_match:
        return "conditional_coexistence"
    if (
        _explicit_contradiction(new_claim, old_claim)
        or (_polarity(new_claim) != _polarity(old_claim) and overlap >= 0.45)
    ):
        return "conflict"
    if new_tokens < old_tokens or old_tokens < new_tokens:
        return "supplement"
    if sequence >= 0.55 or overlap >= 0.55:
        return "revision"
    return None


def relation_explanation(kind: str) -> str:
    messages = {
        "duplicate": "结论文本和成立前提相同，可能是重复结论。",
        "supplement": "结论共享成立前提，新增内容可以补充已有结论。",
        "revision": "结论共享成立前提但表述或范围发生变化，可能是修订。",
        "conflict": "结论成立前提相同，但主张方向相反，需要用户审核冲突。",
        "conditional_coexistence": "结论相似但成立前提不同，建议作为条件化并存结论。",
    }
    return messages.get(kind, "发现可能相关的结论，需要用户审核。")
