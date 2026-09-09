import pytest

from zhiheng.query.conflicts import explicit_constraint_conflicts
from zhiheng.query.contracts import AnswerClaim


@pytest.mark.parametrize("left,right", [
    ("系统必须启用缓存。", "系统必须禁用缓存。"),
    ("团队必须共享资料。", "团队不得共享资料。"),
    ("账户必须开启审计。", "账户必须关闭审计。"),
])
def test_explicit_opposite_constraints_reference_both_sources(left: str, right: str) -> None:
    conflicts = explicit_constraint_conflicts([
        AnswerClaim(left, ("source-a",)), AnswerClaim(right, ("source-b",)),
    ])
    assert len(conflicts) == 1
    assert "source-a" in conflicts[0] and "source-b" in conflicts[0]
    assert "暂不选定结论" in conflicts[0]


@pytest.mark.parametrize("left,right", [
    ("系统必须启用缓存。", "系统必须启用缓存。"),
    ("开发环境必须启用缓存。", "生产环境必须禁用缓存。"),
    ("系统必须启用缓存。", "如果数据敏感，系统必须禁用缓存。"),
    ("系统必须启用缓存。", "系统是否必须禁用缓存？"),
    ("系统必须启用缓存。", "系统不得禁用缓存。"),
])
def test_different_scope_uncertainty_and_double_negation_not_conflated(
    left: str, right: str,
) -> None:
    assert not explicit_constraint_conflicts([
        AnswerClaim(left, ("source-a",)), AnswerClaim(right, ("source-b",)),
    ])
