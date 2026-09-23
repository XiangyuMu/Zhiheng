from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tests.integration.test_learning_evidence import _loader, _sources
from zhiheng.core.ids import new_id
from zhiheng.evolution.artifacts import artifact_digest, default_release_artifact
from zhiheng.evolution.contracts import EvolutionRole, ReleaseBindingV1, command_context_for_role
from zhiheng.evolution.learning_evidence import ProposalSourceGraphV1
from zhiheng.evolution.releases import ReleaseController

TARGET_COMPONENT = "retrieval.answer_strategy"
FIXED_EVAL_SETS = ("boundary", "migration", "retention", "safety")


def _database_path(tmp_path: Path) -> Path:
    return tmp_path / "zhiheng.db"


def _connection(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_database_path(tmp_path))
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _binding(candidate_id: str, baseline_release_id: str, source_digest: str) -> ReleaseBindingV1:
    return ReleaseBindingV1(
        candidate_id=candidate_id,
        target_component=TARGET_COMPONENT,
        source_evaluation_ids=FIXED_EVAL_SETS,
        source_evidence_refs=(source_digest,),
        validation_report_ref=f"synthetic://validation/{candidate_id}",
        reviewer_decision_ref=f"synthetic://review/{candidate_id}",
        approved_artifact_digest=artifact_digest(default_release_artifact()),
        rollback_target_id=baseline_release_id,
    )


def _source_graph(tmp_path: Path) -> ProposalSourceGraphV1:
    factory, arguments = _sources(tmp_path)
    with factory() as session:
        return _loader().build_graph(session, **arguments)


def test_create_proposal_persists_and_reloads_authentic_source_graph(
    tmp_path: Path,
) -> None:
    graph = _source_graph(tmp_path)
    with _connection(tmp_path) as connection:
        controller = ReleaseController.from_db(connection)
        binding = _binding("source-origin", graph.baseline_release_id, graph.canonical_digest())

        proposal = controller.create_release_proposal(
            binding=binding,
            artifact_payload=default_release_artifact(),
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
            source_graph=graph,
        )

        reloaded = controller.load_proposal_source_graph(proposal.proposal_id)
        assert reloaded.canonical_digest() == graph.canonical_digest()
        assert binding.source_evaluation_ids == FIXED_EVAL_SETS
        origin = json.loads(
            connection.execute(
                """
                SELECT event_json FROM proposal_state_events
                WHERE proposal_id = ? AND previous_state = ''
                """,
                (proposal.proposal_id,),
            ).fetchone()[0]
        )
        assert origin["source_graph_digest"] == graph.canonical_digest()
        assert origin["source_graph"] == json.loads(json.dumps(graph.canonical_payload()))
        assert "中文全文检索" not in json.dumps(origin["source_graph"], ensure_ascii=False)


def test_load_proposal_source_graph_rejects_source_less_legacy_candidate(
    tmp_path: Path,
) -> None:
    graph = _source_graph(tmp_path)
    with _connection(tmp_path) as connection:
        controller = ReleaseController.from_db(connection)
        binding = _binding(
            "legacy-source-less",
            graph.baseline_release_id,
            graph.canonical_digest(),
        )
        proposal = controller.create_release_proposal(
            binding=binding,
            artifact_payload=default_release_artifact(),
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
        )

        with pytest.raises(ValueError, match="source graph required"):
            controller.load_proposal_source_graph(proposal.proposal_id)


def test_create_proposal_rejects_source_graph_for_non_current_baseline(
    tmp_path: Path,
) -> None:
    graph = replace(_source_graph(tmp_path), baseline_release_id="other")
    with _connection(tmp_path) as connection:
        controller = ReleaseController.from_db(connection)
        binding = _binding("wrong-baseline", "other", graph.canonical_digest())
        before = connection.execute("SELECT count(*) FROM evolution_proposals").fetchone()[0]

        with pytest.raises(ValueError, match="rollback target"):
            controller.create_release_proposal(
                binding=binding,
                artifact_payload=default_release_artifact(),
                proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
                source_graph=graph,
            )
        after = connection.execute("SELECT count(*) FROM evolution_proposals").fetchone()[0]
        assert after == before


def test_load_proposal_source_graph_rejects_duplicate_origin_identity(
    tmp_path: Path,
) -> None:
    graph = _source_graph(tmp_path)
    with _connection(tmp_path) as connection:
        controller = ReleaseController.from_db(connection)
        binding = _binding("duplicate-origin", graph.baseline_release_id, graph.canonical_digest())
        proposal = controller.create_release_proposal(
            binding=binding,
            artifact_payload=default_release_artifact(),
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
            source_graph=graph,
        )
        origin = connection.execute(
            """
            SELECT event_json, binding_digest FROM proposal_state_events
            WHERE proposal_id = ? AND previous_state = ''
            """,
            (proposal.proposal_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO proposal_state_events (
              id, proposal_id, previous_state, next_state, actor_role, actor_id,
              binding_digest, event_json
            )
            VALUES (?, ?, '', 'candidate', 'proposer', 'proposer', ?, ?)
            """,
            (new_id(), proposal.proposal_id, origin["binding_digest"], origin["event_json"]),
        )

        with pytest.raises(ValueError, match="one immutable source origin"):
            controller.load_proposal_source_graph(proposal.proposal_id)


def test_create_proposal_inside_caller_transaction_is_rollbackable(
    tmp_path: Path,
) -> None:
    graph = _source_graph(tmp_path)
    connection = _connection(tmp_path)
    try:
        controller = ReleaseController.from_db(connection)
        binding = _binding("caller-owned-tx", graph.baseline_release_id, graph.canonical_digest())
        connection.execute("BEGIN IMMEDIATE")

        proposal = controller.create_release_proposal(
            binding=binding,
            artifact_payload=default_release_artifact(),
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
            source_graph=graph,
        )

        assert connection.in_transaction
        connection.rollback()
        assert (
            connection.execute(
                "SELECT count(*) FROM evolution_proposals WHERE id = ?",
                (proposal.proposal_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_load_proposal_source_graph_rejects_database_source_tampering(
    tmp_path: Path,
) -> None:
    graph = _source_graph(tmp_path)
    with _connection(tmp_path) as connection:
        controller = ReleaseController.from_db(connection)
        binding = _binding("tampered-source", graph.baseline_release_id, graph.canonical_digest())
        proposal = controller.create_release_proposal(
            binding=binding,
            artifact_payload=default_release_artifact(),
            proposer_context=command_context_for_role("proposer", EvolutionRole.PROPOSER),
            source_graph=graph,
        )
        first_support = graph.support_sources[0]
        connection.execute(
            """
            INSERT INTO task_evaluations (
              id, trajectory_id, result_json, process_json, quality_json,
              failure_tags_json, confidence, learning_eligible
            )
            SELECT ?, trajectory_id, result_json, process_json, quality_json,
                   failure_tags_json, confidence, learning_eligible
            FROM task_evaluations
            WHERE id = ?
            """,
            (new_id(), first_support.evaluation_id),
        )

        with pytest.raises(ValueError, match="unambiguous signed evaluation|graph"):
            controller.load_proposal_source_graph(proposal.proposal_id)
