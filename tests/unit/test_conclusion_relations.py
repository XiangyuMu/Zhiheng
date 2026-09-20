from zhiheng.conclusions.relations import relation_kind


def _payload(claim: str, premise: str = "固定时间") -> dict:
    return {"claim": claim, "premises": [{"text": premise, "confirmed": False}]}


def test_relation_kinds_distinguish_duplicate_supplement_revision_and_conflict() -> None:
    base = _payload("每天复习能提高记忆")
    assert relation_kind(_payload("每天复习能提高记忆"), base) == "duplicate"
    assert relation_kind(_payload("每天复习能提高记忆并减少遗忘"), base) == "supplement"
    assert relation_kind(_payload("每天复习可以提高记忆"), base) == "revision"
    assert relation_kind(_payload("每天复习不能提高记忆"), base) == "conflict"
    assert relation_kind(_payload("每天复习有效"), _payload("每天复习无效")) == "conflict"


def test_different_premises_are_conditionally_coexistent() -> None:
    assert (
        relation_kind(_payload("每天复习能提高记忆", "考试前一天"), _payload("每天复习能提高记忆"))
        == "conditional_coexistence"
    )
