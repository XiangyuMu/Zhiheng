from __future__ import annotations

from dataclasses import dataclass, replace

from zhiheng.retrieval.contracts import QueryRoute, RouteDecision


@dataclass(frozen=True)
class ProfileIntent:
    matched: bool
    confidence: str
    selector: str | None
    value: str | None
    reason_code: str


class ProfileIntentClassifier:
    """Deterministic, allow-listed natural-language profile classifier."""

    _PATTERNS = (
        (("学校", "大学", "读博", "博士", "学历"), "role.education", "education"),
        (("职业", "工作", "身份", "是谁", "基本信息"), "role.current", "identity"),
        (("目标", "计划", "想做什么"), "goal.personal", "goal"),
        (("项目", "正在做"), "project.current", "project"),
        (("约束", "限制", "隐私偏好"), "constraint.personal", "constraint"),
        (("偏好", "喜欢", "习惯"), "preference.response_style", "preference"),
    )

    def classify(self, query: str) -> ProfileIntent:
        normalized = query.strip().lower()
        if normalized.startswith(("profile:", "memory:")):
            value = normalized.split(":", 1)[1].strip()
            return ProfileIntent(True, "high", "memory.state_key", value, "explicit_profile_prefix")
        for words, selector, reason in self._PATTERNS:
            if any(word in normalized for word in words) and (
                "我" in normalized or "我的" in normalized
            ):
                return ProfileIntent(
                    True, "high", "memory.state_key", selector, f"natural_profile_{reason}"
                )
        return ProfileIntent(False, "none", None, None, "no_profile_intent")


class QueryRouter:
    def route(
        self,
        query: str,
        *,
        selector: str | None = None,
        intent: str | None = None,
        route_override: QueryRoute | None = None,
    ) -> RouteDecision:
        decision = self._base_route(query, selector=selector, intent=intent)
        if selector is None and route_override is not None:
            return replace(
                decision,
                route=route_override,
                reason_code=f"release_{route_override.value}_override",
            )
        return decision

    def _base_route(
        self,
        query: str,
        *,
        selector: str | None,
        intent: str | None,
    ) -> RouteDecision:
        if selector is not None:
            return RouteDecision(
                route=QueryRoute.STRUCTURED,
                reason_code="explicit_structured_selector",
                structured_selector=selector,
            )
        normalized = query.strip().lower()
        profile = ProfileIntentClassifier().classify(query)
        if profile.matched and profile.selector and profile.value:
            return RouteDecision(
                route=QueryRoute.STRUCTURED,
                reason_code=profile.reason_code,
                structured_selector=profile.selector,
            )
        if normalized.startswith(("memory:", "knowledge:")):
            selector_name = (
                "memory.state_key" if normalized.startswith("memory:") else "knowledge.id"
            )
            return RouteDecision(
                route=QueryRoute.STRUCTURED,
                reason_code="selector_prefix",
                structured_selector=selector_name,
            )
        if intent in {"decision", "complex_synthesis"}:
            return RouteDecision(route=QueryRoute.AGENTIC, reason_code=f"intent_{intent}")
        complex_markers = ("权衡", "决策", "计划", "tradeoff")
        if len(normalized) > 120 or any(marker in normalized for marker in complex_markers):
            return RouteDecision(route=QueryRoute.AGENTIC, reason_code="complexity_threshold")
        return RouteDecision(route=QueryRoute.HYBRID, reason_code="default_hybrid")
