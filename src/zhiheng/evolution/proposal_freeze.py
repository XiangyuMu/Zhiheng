"""Freeze maintenance drafts into authenticated strategy proposals."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from zhiheng.evolution.artifacts import (
    artifact_digest,
    default_release_artifact,
    validate_strategy_artifact,
)
from zhiheng.evolution.contracts import EvolutionCommandContext, EvolutionRole
from zhiheng.evolution.learning_evidence import LearningEvidenceLoader
from zhiheng.evolution.releases import ReleaseController, ReleaseProposal, _RawSQLiteSessionAdapter


def verify_persisted_proposal_source_graph(
    connection: Any,
    *,
    proposal_id: str,
    deployment_secret: str,
) -> None:
    """Authoritatively reload proposal provenance through the application port.

    HTTP adapters call this port instead of reaching into the release controller;
    the controller remains an internal domain implementation detail.
    """
    raw = cast(sqlite3.Connection, getattr(connection, "driver_connection", connection))
    ReleaseController.from_db(
        raw, deployment_secret=deployment_secret,
    ).load_proposal_source_graph(proposal_id)


class StrategyProposalService:
    """Turn a persisted draft into one immutable proposer-origin proposal.

    Caller payloads never supply evidence IDs, artifact contents, risk, or state.
    """

    def __init__(self, connection: sqlite3.Connection, *, deployment_secret: str) -> None:
        self._connection = cast(
            sqlite3.Connection, getattr(connection, "driver_connection", connection)
        )
        self._connection.row_factory = sqlite3.Row
        self._deployment_secret = deployment_secret

    def freeze_draft(
        self,
        draft_id: str,
        *,
        proposer_context: EvolutionCommandContext,
        idempotency_key: str,
    ) -> Any:
        if proposer_context.role is not EvolutionRole.PROPOSER:
            raise ValueError("freezing requires proposer capability")
        if not idempotency_key:
            raise ValueError("freeze requires idempotency key")
        adapter = _RawSQLiteSessionAdapter(self._connection)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            replay = self._connection.execute(
                """SELECT proposal_id FROM proposal_state_events
                   WHERE json_extract(event_json, '$.freeze_idempotency_key') = ?""",
                (idempotency_key,),
            ).fetchone()
            if replay is not None:
                proposer = self._connection.execute(
                    "SELECT proposer_id FROM evolution_proposals WHERE id=?", (replay[0],)
                ).fetchone()
                return ReleaseProposal(proposal_id=replay[0], proposer_id=proposer[0])
            row = self._connection.execute(
                """SELECT artifact_json, artifact_kind, status
                   FROM evolution_artifacts WHERE id=?""",
                (draft_id,),
            ).fetchone()
            if row is None or row[1] != "strategy_proposal_draft" or row[2] != "draft":
                raise ValueError("only a draft strategy artifact can be frozen")
            payload = json.loads(row[0])
            if payload.get("target_component") != "retrieval.answer_strategy":
                raise ValueError("draft target component is not supported")
            failure_tag = payload.get("failure_tag")
            if not isinstance(failure_tag, str) or not failure_tag:
                raise ValueError("draft requires a persisted failure tag")
            controller = ReleaseController.from_db(
                self._connection, deployment_secret=self._deployment_secret,
            )
            baseline = controller.load_default_head("retrieval.answer_strategy")
            if baseline is None:
                raise ValueError("cannot freeze without stable baseline")
            graph = LearningEvidenceLoader(self._deployment_secret).build_graph_for_failure(
                cast(Any, adapter),
                baseline_release_id=baseline.release_id,
                baseline_binding_digest=baseline.binding_digest,
                baseline_artifact_digest=baseline.binding.approved_artifact_digest,
                failure_tag=failure_tag,
            )
            artifact = default_release_artifact()
            validate_strategy_artifact(artifact)
            from zhiheng.evolution.contracts import ReleaseBindingV1
            binding = ReleaseBindingV1(
                candidate_id=draft_id, target_component="retrieval.answer_strategy",
                source_evaluation_ids=("boundary", "migration", "retention", "safety"),
                source_evidence_refs=(draft_id,), validation_report_ref="pending",
                reviewer_decision_ref="pending", approved_artifact_digest=artifact_digest(artifact),
                rollback_target_id=baseline.release_id,
            )
            proposal = controller.create_release_proposal(
                binding=binding, proposer_context=proposer_context,
                artifact_payload=artifact, source_graph=graph,
            )
            self._connection.execute(
                """UPDATE proposal_state_events
                   SET event_json=json_set(event_json, '$.freeze_idempotency_key', ?)
                   WHERE proposal_id=? AND previous_state=''""",
                (idempotency_key, proposal.proposal_id),
            )
            return proposal
