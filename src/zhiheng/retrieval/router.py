from __future__ import annotations

from dataclasses import replace

from zhiheng.retrieval.contracts import QueryRoute, RouteDecision


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
                decision, route=route_override,
                reason_code=f"release_{route_override.value}_override",
            )
        return decision

    def _base_route(
        self, query: str, *, selector: str | None, intent: str | None,
    ) -> RouteDecision:
        if selector is not None:
            return RouteDecision(
                route=QueryRoute.STRUCTURED,
                reason_code="explicit_structured_selector",
                structured_selector=selector,
            )
        normalized = query.strip().lower()
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
