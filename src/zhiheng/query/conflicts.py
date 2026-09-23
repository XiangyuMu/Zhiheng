"""Conservative exact-clause conflicts for the offline extractive answer mode.

This is not semantic/NLI inference. Different subjects, scopes or conditional
clauses are not merged; callers must disclose the remaining semantic uncertainty.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from zhiheng.query.contracts import AnswerClaim

_CLAUSE = re.compile(
    r"^(?P<scope>[^。！？!?；;\n]{1,80}?)"
    r"(?P<modal>必须|不得|禁止)(?P<action>[^。！？!?；;\n]{1,80})[。.]?$"
)
_CONDITIONAL = ("如果", "假如", "假设", "是否", "可能", "例如", "据说", "当", "若")
_OPPOSITES = {"禁用": "启用", "关闭": "开启", "禁止": "允许"}


def explicit_constraint_conflicts(claims: Sequence[AnswerClaim]) -> tuple[str, ...]:
    seen: dict[tuple[str, str], dict[bool, set[str]]] = {}
    for claim in claims:
        if not claim.citation_ids:
            continue
        for clause in re.split(r"(?<=[。；;\n])", claim.text):
            clause = clause.strip().rstrip("；;")
            if any(marker in clause for marker in _CONDITIONAL):
                continue
            match = _CLAUSE.fullmatch(clause)
            if match is None:
                continue
            scope = re.sub(r"\s+", "", match["scope"])
            action = re.sub(r"\s+", "", match["action"].rstrip("。."))
            positive = match["modal"] == "必须"
            for negative, affirmative in _OPPOSITES.items():
                if action.startswith(negative):
                    action = affirmative + action[len(negative) :]
                    positive = not positive
                    break
            polarities = seen.setdefault((scope, action), {})
            polarities.setdefault(positive, set()).update(claim.citation_ids)
    conflicts = []
    for (scope, action), polarities in sorted(seen.items()):
        if True in polarities and False in polarities:
            citations = sorted(polarities[True] | polarities[False])
            conflicts.append(
                f"潜在约束冲突：{scope}对“{action}”存在相反要求；"
                f"请核对适用范围和时间，暂不选定结论。引用：{', '.join(citations)}"
            )
    return tuple(conflicts)
