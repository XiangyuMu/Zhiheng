from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text

from tests.integration.test_learning_evidence import _sources
from zhiheng.core.config import Settings
from zhiheng.evolution.contracts import EvolutionRole, command_context_for_role
from zhiheng.evolution.proposal_freeze import StrategyProposalService


def test_freeze_draft_reloads_persisted_sources_and_replays_idempotently(tmp_path: Path) -> None:
    factory, arguments = _sources(tmp_path)
    with factory() as session:
        row = session.execute(
            text("""INSERT INTO evolution_artifacts
               (id, artifact_kind, binding_digest, artifact_digest,
                artifact_json, status, source_ref)
               VALUES ('draft-1', 'strategy_proposal_draft', 'x', 'y', :payload, 'draft', NULL)"""),
            {"payload": json.dumps({
                "output_type": "strategy_proposal_draft",
                "target_component": "retrieval.answer_strategy",
                "failure_tag": "not-a-persisted-failure",
            })},
        )
        del row
        session.commit()
        raw = session.connection().connection
        service = StrategyProposalService(
            cast(Any, raw),
            deployment_secret=Settings(environment="test").secret_key.get_secret_value(),
        )
        context = command_context_for_role("synthetic-proposer", EvolutionRole.PROPOSER)
        # This draft intentionally has insufficient persisted support evidence;
        # freezing must fail closed instead of accepting caller evidence_refs.
        try:
            service.freeze_draft("draft-1", proposer_context=context, idempotency_key="freeze-1")
        except ValueError as exc:
            assert "evidence" in str(exc)
        else:
            raise AssertionError("incomplete persisted evidence was frozen")
