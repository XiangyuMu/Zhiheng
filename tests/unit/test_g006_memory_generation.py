from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.api.retrieval import EvidenceBoundAnswerModel
from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_memory_generation import execute_memory_generation_probe
from zhiheng.memory import MemoryCandidateInput, MemoryRepository, MemoryValue
from zhiheng.query.contracts import AnswerClaim, GeneratedAnswer, PersonalizationRef
from zhiheng.retrieval import QueryRoute


def test_memory_generation_probe_uses_formal_context_and_separate_knowledge(
    tmp_path: Path,
) -> None:
    settings, factory = _migrated_memory_fixture(tmp_path)

    facts, outcomes = execute_memory_generation_probe(
        factory, settings, route_override=QueryRoute.HYBRID
    )

    assert all(outcomes.values())
    assert facts["answer_route"] == "hybrid"
    assert facts["serving_release_available"] is True
    assert facts["formal_context_contains_goal_fixed"] is True
    assert facts["model_call_count"] == 1
    assert facts["knowledge_citation_count"] >= 1
    assert facts["personalization_ref_count"] >= 1
    assert facts["execution_profile"] == (
        "real_answer_dispatch_generation_context_local_extractive"
    )
    assert "confirmed-probe" not in json.dumps(facts, ensure_ascii=False)


def test_memory_generation_probe_fails_if_model_leaks_pending_context_or_refs(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    settings, factory = _migrated_memory_fixture(tmp_path)

    def leaking_generate_answer(self: Any, **kwargs: Any) -> GeneratedAnswer:
        return GeneratedAnswer(
            answer="unconfirmed-goal-sentinel",
            claims=(AnswerClaim("unconfirmed-goal-sentinel", ("forged-citation",)),),
            personalization_refs=(
                PersonalizationRef(
                    "pending-candidate-forged",
                    "pending-version-forged",
                    0,
                    "goal.pending",
                ),
            ),
        )

    monkeypatch.setattr(
        EvidenceBoundAnswerModel,
        "generate_answer",
        leaking_generate_answer,
    )

    facts, outcomes = execute_memory_generation_probe(
        factory, settings, route_override=QueryRoute.HYBRID
    )

    assert not all(outcomes.values())
    assert outcomes["memory.pending_response_zero"] is False
    assert outcomes["memory.personalization_refs_authorized_only"] is False
    assert facts["model_call_count"] == 1


def _migrated_memory_fixture(
    tmp_path: Path,
) -> tuple[Settings, sessionmaker[Session]]:
    db_path = tmp_path / "zhiheng.sqlite"
    object_store_path = tmp_path / "objects"
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        knowledge_object_store_path=str(object_store_path),
    )
    config = Config(str(Path.cwd() / "alembic.ini"))
    config.set_main_option("script_location", str(Path.cwd() / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(settings)
    factory = create_session_factory(engine)
    repository = MemoryRepository()
    with factory.begin() as session:
        repository.commit_explicit_memory(
            session,
            MemoryValue("goal", "goal.fixed", {"text": "confirmed-probe"}),
            operation_key="fixed-generation-seed",
        )
        repository.propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="goal",
                state_key="goal.fixed",
                proposed_value={"text": "unconfirmed-sentinel"},
                rationale="protected synthetic fixture",
                source_kind="agent_inferred",
                confidence=0.7,
            ),
        )
        repository.propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="profile",
                state_key="goal.unconfirmed",
                proposed_value={"text": "unconfirmed-profile-sentinel"},
                rationale="protected synthetic fixture",
                source_kind="agent_inferred",
                confidence=0.7,
            ),
        )
        repository.propose_candidate(
            session,
            MemoryCandidateInput(
                candidate_type="inferred",
                memory_type="goal",
                state_key="goal.pending",
                proposed_value={"text": "unconfirmed-goal-sentinel"},
                rationale="protected synthetic fixture",
                source_kind="agent_inferred",
                confidence=0.7,
            ),
        )
    return settings, factory
