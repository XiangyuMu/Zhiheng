from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self, cast

from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_json, sha256_text
from zhiheng.evaluation.execution_records import (
    require_passing_execution,
    verify_execution_record,
)
from zhiheng.evaluation.g006_evolution import EvaluationReport, evaluate_g006_evolution
from zhiheng.evaluation.g006_registry import (
    REGISTERED_FIXED_CASE_IDS,
    REGISTERED_FIXED_SET_NAMES,
    assert_registered_fixed_case,
    assert_required_fixed_case_coverage,
)
from zhiheng.evolution.artifacts import (
    artifact_digest,
    parse_artifact_json,
    validate_artifact_digest,
    validate_serving_strategy_artifact,
    validate_strategy_artifact,
)
from zhiheng.evolution.contracts import (
    EvolutionCapability,
    EvolutionCommandContext,
    EvolutionRole,
    ReleaseBindingV1,
    ReleaseState,
    command_context_for_role,
)
from zhiheng.evolution.learning_evidence import LearningEvidenceLoader, ProposalSourceGraphV1
from zhiheng.evolution.state_machine import EvolutionStateMachine
from zhiheng.evolution.trajectories import TrajectoryEnvelopeV1

_FIXED_EVAL_SETS = ("boundary", "migration", "retention", "safety")
_CANARY_MIN_SAMPLES = 5
_PROMOTION_EVAL_SCHEMA = "g006.canonical_evaluation_report.v2"
_POLICY_SNAPSHOT_SCHEMA = "g006.release_policy_snapshot.v1"


@dataclass(frozen=True, slots=True)
class CanaryAssignment:
    scope: dict[str, Any]
    expires_at: str

    def canonical_payload(self) -> dict[str, Any]:
        return {"scope": self.scope, "expires_at": self.expires_at}


@dataclass(frozen=True, slots=True)
class CanaryObservationSummary:
    sample_size: int
    evidence_digest: str

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "evidence_digest": self.evidence_digest,
            "sample_size": self.sample_size,
        }


@dataclass(frozen=True, slots=True)
class ReleaseValidationEvidence:
    proposal_id: str
    validation_report_id: str
    evaluation_report_digest: str
    policy_snapshot_digest: str
    source_evaluation_ids: tuple[str, ...]
    source_trajectory_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReleaseProposal:
    proposal_id: str
    proposer_id: str


@dataclass(frozen=True, slots=True)
class ReleaseReviewEvidence:
    review_report_id: str


@dataclass(frozen=True, slots=True)
class ReleaseTransitionAudit:
    event_id: str
    release_id: str
    previous_state: ReleaseState
    next_state: ReleaseState
    actor_role: EvolutionRole
    actor_id: str
    request_id: str


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    release_id: str
    target_component: str
    state: ReleaseState
    binding: ReleaseBindingV1
    binding_digest: str
    rollback_target_id: str | None
    canary_assignment: CanaryAssignment | None
    transition_audit: tuple[ReleaseTransitionAudit, ...] = ()

    @property
    def is_default_head(self) -> bool:
        return self.state is ReleaseState.STABLE

    @property
    def is_user_visible(self) -> bool:
        return self.state is ReleaseState.STABLE or (
            self.state is ReleaseState.CANARY and self.canary_assignment is not None
        )


class ReleaseController:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        state_machine: EvolutionStateMachine | None = None,
        deployment_secret: str | None = None,
    ) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._state_machine = state_machine or EvolutionStateMachine()
        self._deployment_secret = (
            Settings().secret_key.get_secret_value()
            if deployment_secret is None
            else deployment_secret
        )
        if not self._deployment_secret:
            raise ValueError("trajectory verification requires a deployment secret")

    @classmethod
    def from_db(
        cls,
        connection: sqlite3.Connection,
        *,
        state_machine: EvolutionStateMachine | None = None,
        deployment_secret: str | None = None,
    ) -> Self:
        return cls(
            connection,
            state_machine=state_machine,
            deployment_secret=deployment_secret,
        )

    def load_default_head(self, target_component: str) -> ReleaseContext | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM strategy_releases
            WHERE target_component = ? AND state = 'stable'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (target_component,),
        ).fetchone()
        if row is None:
            return None
        return self._load_context(cast(sqlite3.Row, row))

    def load_release(self, release_id: str) -> ReleaseContext:
        row = self._connection.execute(
            "SELECT * FROM strategy_releases WHERE id = ?",
            (release_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"release not found: {release_id}")
        return self._load_context(cast(sqlite3.Row, row))

    def load_history(self, release_id: str) -> tuple[ReleaseTransitionAudit, ...]:
        rows = self._connection.execute(
            """
            SELECT id, previous_state, next_state, actor_role, actor_id, event_json
            FROM release_transition_events
            WHERE release_id = ?
            ORDER BY created_at, id
            """,
            (release_id,),
        ).fetchall()
        audits: list[ReleaseTransitionAudit] = []
        for row in rows:
            payload = _json_obj(cast(sqlite3.Row, row)["event_json"])
            audits.append(
                ReleaseTransitionAudit(
                    event_id=cast(sqlite3.Row, row)["id"],
                    release_id=release_id,
                    previous_state=ReleaseState(cast(sqlite3.Row, row)["previous_state"]),
                    next_state=ReleaseState(cast(sqlite3.Row, row)["next_state"]),
                    actor_role=EvolutionRole(cast(sqlite3.Row, row)["actor_role"]),
                    actor_id=str(cast(sqlite3.Row, row)["actor_id"] or ""),
                    request_id=str(payload.get("request_id", "")),
                )
            )
        return tuple(audits)

    def bootstrap_stable_release(
        self,
        *,
        binding: ReleaseBindingV1,
        proposer_id: str,
        reviewer_id: str,
        reviewer_decision_ref: str,
        validation_report_ref: str,
        canary_assignment: CanaryAssignment,
        canary_samples: int,
        request_id: str,
        approved_artifact_digest: str | None = None,
    ) -> ReleaseContext:
        self._validate_binding(binding)
        self._validate_reviewers(proposer_id=proposer_id, reviewer_id=reviewer_id)
        self._validate_fixed_sets(binding)
        self._validate_canary_assignment(canary_assignment)
        self._validate_canary_samples(canary_samples)

        current_stable = self.load_default_head(binding.target_component)
        if current_stable is not None:
            if current_stable.binding.canonical_digest() != binding.canonical_digest():
                raise ValueError("trusted baseline stable binding mismatch")
            if len(current_stable.transition_audit) != 1:
                raise ValueError("trusted baseline stable request mismatch")
            bootstrap_event = current_stable.transition_audit[0]
            if bootstrap_event.request_id != request_id:
                raise ValueError("trusted baseline stable request mismatch")
            if bootstrap_event.previous_state is not ReleaseState.PREPARED:
                raise ValueError("trusted baseline stable already exists for this target component")
            if bootstrap_event.next_state is not ReleaseState.STABLE:
                raise ValueError("trusted baseline stable already exists for this target component")
            return current_stable

        raise ValueError(
            "no trusted baseline release exists; run the migration seed before bootstrapping"
        )

    def prepare_release(
        self,
        *,
        binding: ReleaseBindingV1,
        canary_assignment: CanaryAssignment,
        canary_samples: int,
        request_id: str,
        validation_report_id: str | None = None,
        review_report_id: str | None = None,
        evaluation_report_digest: str | None = None,
        policy_snapshot_digest: str | None = None,
        proposer_id: str | None = None,
        reviewer_id: str | None = None,
        reviewer_decision_ref: str | None = None,
        validation_report_ref: str | None = None,
    ) -> ReleaseContext:
        self._validate_binding(binding)
        self._validate_fixed_sets(binding)
        self._validate_canary_assignment(canary_assignment)
        self._validate_canary_samples(canary_samples)
        if validation_report_id is None or review_report_id is None:
            raise ValueError("prepare requires persisted validation and review evidence")
        if evaluation_report_digest is None or policy_snapshot_digest is None:
            raise ValueError("prepare requires persisted evidence digests")
        if proposer_id is None or reviewer_id is None:
            raise ValueError("prepare requires proposer and reviewer identities")
        if reviewer_decision_ref is None or validation_report_ref is None:
            raise ValueError("prepare requires review and validation references")
        current_stable = self.load_default_head(binding.target_component)
        if current_stable is None:
            raise ValueError(f"no current stable release for {binding.target_component}")
        if binding.rollback_target_id != current_stable.release_id:
            raise ValueError("rollback target must be the current stable release")

        existing = self._find_release_by_request(request_id)
        if existing is not None:
            if existing.binding.canonical_digest() != binding.canonical_digest():
                raise ValueError("idempotency key reused for a different release")
            return existing

        evidence = self._validate_prepared_evidence(
            binding=binding,
            validation_report_id=validation_report_id,
            review_report_id=review_report_id,
            evaluation_report_digest=evaluation_report_digest,
            policy_snapshot_digest=policy_snapshot_digest,
            canary_samples=canary_samples,
            proposer_id=proposer_id,
            reviewer_id=reviewer_id,
            reviewer_decision_ref=reviewer_decision_ref,
            validation_report_ref=validation_report_ref,
        )
        return self._create_release(
            binding=binding,
            proposal_id=str(evidence["proposal_id"]),
            validation_report_id=validation_report_id,
            review_report_id=review_report_id,
            release_gate=_json_mapping(evidence["release_gate"]),
            source_evaluation_ids=tuple(str(item) for item in evidence["source_evaluation_ids"]),
            source_trajectory_ids=tuple(str(item) for item in evidence["source_trajectory_ids"]),
            proposer_id=str(evidence["proposer_id"]),
            reviewer_id=str(evidence["reviewer_id"]),
            canary_assignment=canary_assignment,
            canary_samples=canary_samples,
            request_id=request_id,
            initial_state=ReleaseState.PREPARED,
        )

    def create_release_proposal(
        self,
        *,
        binding: ReleaseBindingV1,
        proposer_context: EvolutionCommandContext,
        artifact_payload: dict[str, Any],
        source_graph: ProposalSourceGraphV1 | None = None,
    ) -> ReleaseProposal:
        self._state_machine.validate_command_context(
            proposer_context,
            required_role=EvolutionRole.PROPOSER,
            required_capability=EvolutionCapability.PROPOSE,
        )
        self._validate_binding(binding)
        _validated_artifact_payload_digest(binding, artifact_payload)
        source_graph_payload: dict[str, Any] | None = None
        source_graph_digest: str | None = None
        if source_graph is not None:
            self._validate_source_graph_baseline(binding=binding, source_graph=source_graph)
            source_graph_payload = source_graph.canonical_payload()
            source_graph_digest = source_graph.canonical_digest()
        proposal_id = new_id()
        with self._proposal_creation_transaction():
            event_json = {
                "binding_digest": binding.canonical_digest(),
                "target_component": binding.target_component,
                "binding": binding.canonical_payload(),
                "candidate_artifact": artifact_payload,
                "candidate_artifact_digest": binding.approved_artifact_digest,
            }
            if source_graph_payload is not None and source_graph_digest is not None:
                event_json["source_graph"] = source_graph_payload
                event_json["source_graph_digest"] = source_graph_digest
            self._connection.execute(
                """
                INSERT INTO evolution_proposals (
                  id, target_component, state, risk_level, minimal_diff_json,
                  support_refs_json, counter_refs_json, proposer_id
                )
                VALUES (?, ?, 'candidate', 'low', ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    binding.target_component,
                    _json_text({"binding_digest": binding.canonical_digest()}),
                    _json_text(list(binding.source_evidence_refs)),
                    "[]",
                    proposer_context.actor_id,
                ),
            )
            self._insert_proposal_state_event(
                proposal_id=proposal_id,
                previous_state=None,
                next_state="candidate",
                actor_role=EvolutionRole.PROPOSER,
                actor_id=proposer_context.actor_id,
                binding_digest=binding.canonical_digest(),
                event_json=event_json,
            )
        return ReleaseProposal(
            proposal_id=proposal_id,
            proposer_id=proposer_context.actor_id,
        )

    def load_release_artifact(self, release_id: str) -> dict[str, Any]:
        release = self.load_release(release_id)
        return self._load_verified_release_artifact(
            binding_digest=release.binding_digest,
            artifact_digest=release.binding.approved_artifact_digest,
        )

    def load_proposal_binding(self, proposal_id: str) -> ReleaseBindingV1:
        rows = self._connection.execute(
            "SELECT event_json FROM proposal_state_events WHERE proposal_id = ? "
            "AND previous_state = '' AND next_state = 'candidate' AND actor_role = 'proposer'",
            (proposal_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("proposal requires one immutable binding origin")
        payload = dict(_json_obj(rows[0]["event_json"])["binding"])
        payload["source_evaluation_ids"] = tuple(payload["source_evaluation_ids"])
        payload["source_evidence_refs"] = tuple(payload["source_evidence_refs"])
        binding = ReleaseBindingV1(**payload)
        self._validate_binding(binding)
        self.load_proposal_artifact(proposal_id, binding)
        return binding

    def load_proposal_artifact(
        self, proposal_id: str, binding: ReleaseBindingV1
    ) -> dict[str, Any]:
        """Load the immutable candidate content recorded before validation starts."""
        rows = self._connection.execute(
            """
            SELECT pse.event_json, pse.actor_id, ep.proposer_id, pse.binding_digest
            FROM proposal_state_events pse
            JOIN evolution_proposals ep ON ep.id = pse.proposal_id
            WHERE pse.proposal_id = ? AND pse.previous_state = ''
              AND pse.next_state = 'candidate' AND pse.actor_role = 'proposer'
            """,
            (proposal_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("proposal requires one immutable candidate artifact origin")
        row = rows[0]
        if (
            row["actor_id"] != row["proposer_id"]
            or row["binding_digest"] != binding.canonical_digest()
        ):
            raise ValueError("proposal artifact origin identity or binding mismatch")
        origin = _json_obj(row["event_json"])
        if origin.get("binding") != binding.canonical_payload():
            raise ValueError("proposal artifact full binding mismatch")
        payload = origin.get("candidate_artifact")
        if not isinstance(payload, dict):
            raise ValueError("proposal candidate artifact must predate evaluation")
        if origin.get("candidate_artifact_digest") != binding.approved_artifact_digest:
            raise ValueError("proposal artifact digest binding mismatch")
        _validated_artifact_payload_digest(binding, payload)
        return cast(dict[str, Any], payload)

    def load_proposal_source_graph(self, proposal_id: str) -> ProposalSourceGraphV1:
        """Load and re-verify the immutable learning-source origin for a proposal."""
        row = self._load_unique_proposer_origin(proposal_id)
        if (
            row["actor_id"] != row["proposer_id"]
            or row["binding_digest"] != row["minimal_binding_digest"]
        ):
            raise ValueError("proposal source origin identity or binding mismatch")
        origin = _json_obj(row["event_json"])
        source_graph_payload = origin.get("source_graph")
        source_graph_digest = origin.get("source_graph_digest")
        if not isinstance(source_graph_payload, dict) or not isinstance(source_graph_digest, str):
            raise ValueError("proposal source graph required for candidate provenance")
        binding_payload = origin.get("binding")
        if not isinstance(binding_payload, dict):
            raise ValueError("proposal source graph requires immutable binding origin")
        binding_data = dict(binding_payload)
        binding_data["source_evaluation_ids"] = tuple(binding_data["source_evaluation_ids"])
        binding_data["source_evidence_refs"] = tuple(binding_data["source_evidence_refs"])
        binding = ReleaseBindingV1(**binding_data)
        self._validate_binding(binding)
        if binding.canonical_digest() != row["binding_digest"]:
            raise ValueError("proposal source graph binding digest mismatch")
        self.load_proposal_artifact(proposal_id, binding)
        graph = LearningEvidenceLoader(self._deployment_secret).verify_graph(
            cast(Any, _RawSQLiteSessionAdapter(self._connection)),
            source_graph_payload,
            source_graph_digest,
        )
        self._validate_source_graph_baseline(binding=binding, source_graph=graph)
        if json.loads(_json_text(graph.canonical_payload())) != source_graph_payload:
            raise ValueError("proposal source graph canonical payload mismatch")
        return graph

    @contextmanager
    def _proposal_creation_transaction(self) -> Iterator[None]:
        """Own only the creation slice; never commit a caller-owned transaction."""
        owns_transaction = not self._connection.in_transaction
        savepoint = f"proposal_create_{new_id().replace('-', '_')}"
        if owns_transaction:
            self._connection.execute("BEGIN")
        else:
            self._connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except Exception:
            if owns_transaction:
                self._connection.rollback()
            else:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            if owns_transaction:
                self._connection.commit()
            else:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _load_unique_proposer_origin(self, proposal_id: str) -> sqlite3.Row:
        rows = self._connection.execute(
            """
            SELECT pse.event_json, pse.actor_id, ep.proposer_id, pse.binding_digest,
                   json_extract(ep.minimal_diff_json, '$.binding_digest') AS minimal_binding_digest
            FROM proposal_state_events pse
            JOIN evolution_proposals ep ON ep.id = pse.proposal_id
            WHERE pse.proposal_id = ? AND pse.previous_state = ''
              AND pse.next_state = 'candidate' AND pse.actor_role = 'proposer'
            """,
            (proposal_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("proposal requires one immutable source origin")
        return cast(sqlite3.Row, rows[0])

    def _validate_source_graph_baseline(
        self, *, binding: ReleaseBindingV1, source_graph: ProposalSourceGraphV1,
    ) -> None:
        if source_graph.task_family != binding.target_component:
            raise ValueError("proposal source graph target component mismatch")
        current_stable = self.load_default_head(binding.target_component)
        if current_stable is None:
            raise ValueError("proposal source graph requires a current stable baseline")
        if binding.rollback_target_id != current_stable.release_id:
            raise ValueError("proposal rollback target must match source graph baseline")
        if (
            source_graph.baseline_release_id != current_stable.release_id
            or source_graph.baseline_binding_digest != current_stable.binding_digest
            or source_graph.baseline_artifact_digest
            != current_stable.binding.approved_artifact_digest
        ):
            raise ValueError("proposal source graph baseline mismatch")

    def validate_executed_proposal(
        self, *, proposal_id: str, evaluation_run_id: str,
        validator_context: EvolutionCommandContext,
    ) -> ReleaseValidationEvidence:
        """Publish verified evaluation evidence idempotently, without approving a release."""
        self._state_machine.validate_command_context(
            validator_context, required_role=EvolutionRole.VALIDATOR,
            required_capability=EvolutionCapability.VALIDATE,
        )
        if self._connection.in_transaction:
            raise ValueError("proposal validation requires a transaction-free command connection")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            binding = self.load_proposal_binding(proposal_id)
            # The worker is an independent command path; never rely on the HTTP
            # preflight for provenance. Reload the frozen graph from authenticated
            # trajectory records before accepting any evaluator result.
            self.load_proposal_source_graph(proposal_id)
            rows = self._connection.execute(
                "SELECT * FROM validation_reports WHERE proposal_id = ?", (proposal_id,),
            ).fetchall()
            if rows:
                if len(rows) != 1 or rows[0]["status"] != "approved":
                    raise ValueError("proposal validation history is not uniquely approved")
                row = rows[0]
                report = _json_obj(row["fixed_set_result_json"])
                policy = _json_obj(row["latency_cost_json"])
                if report.get("execution_run_id") != evaluation_run_id:
                    raise ValueError("proposal already validated with another execution run")
                self._validate_report_execution(report, proposal_id=proposal_id, binding=binding)
                _validate_canonical_promotion_report(report, binding=binding)
                _validate_dynamic_report(_json_obj(row["dynamic_set_result_json"]))
                _validate_policy_snapshot(policy, canary_samples=_CANARY_MIN_SAMPLES)
                if policy.get("evaluation_report_digest") != report.get("report_digest"):
                    raise ValueError("validation policy report digest mismatch")
                return ReleaseValidationEvidence(
                    proposal_id=proposal_id, validation_report_id=str(row["id"]),
                    evaluation_report_digest=str(report["report_digest"]),
                    policy_snapshot_digest=str(policy["snapshot_digest"]),
                    source_evaluation_ids=tuple(_case_ids_from_report(report)),
                    source_trajectory_ids=tuple(policy["source_trajectory_ids"]),
                )
            execution_row = self._connection.execute(
                "SELECT * FROM proposal_execution_runs WHERE id = ?", (evaluation_run_id,),
            ).fetchone()
            if execution_row is None:
                raise ValueError("protected proposal execution run not found")
            execution = verify_execution_record(dict(execution_row), secret=self._deployment_secret)
            return self.record_release_validation_evidence(
                binding=binding, proposal_id=proposal_id,
                validation_report_ref=binding.validation_report_ref,
                canary_samples=_CANARY_MIN_SAMPLES, validator_context=validator_context,
                trajectory_ids=execution.get("trajectory_ids", []),
                evaluation_run_id=evaluation_run_id,
            )

    def record_release_validation_evidence(
        self,
        *,
        binding: ReleaseBindingV1,
        proposal_id: str,
        validation_report_ref: str,
        canary_samples: int,
        validator_context: EvolutionCommandContext,
        trajectory_ids: Sequence[str],
        evaluation_run_id: str | None = None,
    ) -> ReleaseValidationEvidence:
        self._state_machine.validate_command_context(
            validator_context,
            required_role=EvolutionRole.VALIDATOR,
            required_capability=EvolutionCapability.VALIDATE,
        )
        self._validate_binding(binding)
        self._validate_fixed_sets(binding)
        self._validate_canary_samples(canary_samples)
        if validation_report_ref != binding.validation_report_ref:
            raise ValueError("validation report ref must match release binding")
        proposal = self._connection.execute(
            """
            SELECT proposer_id, target_component, state, minimal_diff_json
            FROM evolution_proposals
            WHERE id = ?
            """,
            (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise ValueError("validation requires persisted proposal")
        if proposal["target_component"] != binding.target_component:
            raise ValueError("validation proposal target component mismatch")
        if proposal["state"] != "candidate":
            raise ValueError("validation proposal must be a candidate")
        minimal_diff = _json_obj(proposal["minimal_diff_json"])
        if minimal_diff.get("binding_digest") != binding.canonical_digest():
            raise ValueError("validation proposal binding digest mismatch")
        origins = self._connection.execute(
            """
            SELECT actor_role, actor_id, binding_digest FROM proposal_state_events
            WHERE proposal_id = ? AND previous_state = '' AND next_state = 'candidate'
            """,
            (proposal_id,),
        ).fetchall()
        if (
            len(origins) != 1
            or origins[0]["actor_role"] != EvolutionRole.PROPOSER.value
            or origins[0]["actor_id"] != proposal["proposer_id"]
            or origins[0]["binding_digest"] != binding.canonical_digest()
        ):
            raise ValueError("validation requires intact Proposer creation evidence")
        validation_id = new_id()
        self.load_proposal_artifact(proposal_id, binding)
        if evaluation_run_id is None:
            raise ValueError("validation requires a protected proposal execution run")
        execution_row = self._connection.execute(
            "SELECT * FROM proposal_execution_runs WHERE id = ?", (evaluation_run_id,)
        ).fetchone()
        if execution_row is None:
            raise ValueError("protected proposal execution run not found")
        execution = verify_execution_record(dict(execution_row), secret=self._deployment_secret)
        require_passing_execution(
            execution, proposal_id=proposal_id, binding_digest=binding.canonical_digest(),
            artifact_digest=binding.approved_artifact_digest,
        )
        executed_ids = execution.get("trajectory_ids")
        if (not isinstance(executed_ids, list) or len(set(trajectory_ids)) != len(trajectory_ids)
            or sorted(trajectory_ids) != sorted(executed_ids)):
            raise ValueError("validation trajectories must match the protected execution run")
        evaluation_cases = self._load_evaluation_cases(
            binding=binding,
            trajectory_ids=trajectory_ids,
        )
        self._validate_execution_case_links(execution, evaluation_cases)
        evaluation_report = evaluate_g006_evolution(
            release_id=f"pending:{binding.candidate_id}",
            set_name="promotion",
            cases=evaluation_cases,
        )
        if evaluation_report.failure_count != 0 or not evaluation_report.promotion_eligible:
            raise ValueError("promotion evaluation report is not eligible")
        for score in evaluation_report.scores.values():
            if score < 1.0:
                raise ValueError("promotion evaluation requires 100 percent scores")
        source_evaluation_ids = tuple(case.case_id for case in evaluation_report.case_assessments)
        assert_required_fixed_case_coverage(source_evaluation_ids)
        source_trajectory_ids = tuple(sorted(set(trajectory_ids)))
        fixed_set_result_json = _canonical_promotion_report(
            binding=binding,
            validation_report_ref=validation_report_ref,
            evaluation_report=evaluation_report,
            execution_run_id=evaluation_run_id,
            execution_record_digest=_digest_mapping(execution),
        )
        latency_cost_json = _canonical_policy_snapshot(
            validation_report_ref=validation_report_ref,
            evaluation_report=evaluation_report,
            promotion_report_digest=str(fixed_set_result_json["report_digest"]),
            canary_samples=canary_samples,
            source_trajectory_ids=source_trajectory_ids,
        )
        with self._connection:
            self._transition_proposal(
                proposal_id=proposal_id,
                previous_state="candidate",
                next_state="evidence_ready",
                actor_role=EvolutionRole.VALIDATOR,
                actor_id=validator_context.actor_id,
                binding_digest=binding.canonical_digest(),
                event_json={"validation_report_ref": validation_report_ref},
            )
            self._transition_proposal(
                proposal_id=proposal_id,
                previous_state="evidence_ready",
                next_state="validating",
                actor_role=EvolutionRole.VALIDATOR,
                actor_id=validator_context.actor_id,
                binding_digest=binding.canonical_digest(),
                event_json={"validation_report_id": validation_id},
            )
            self._connection.execute(
                """
                INSERT INTO validation_reports (
                  id, proposal_id, fixed_set_result_json, dynamic_set_result_json,
                  latency_cost_json, status
                )
                VALUES (?, ?, ?, ?, ?, 'approved')
                """,
                (
                    validation_id,
                    proposal_id,
                    _json_text(fixed_set_result_json),
                    _json_text({"candidate_only": True, "cases": []}),
                    _json_text(latency_cost_json),
                ),
            )
        return ReleaseValidationEvidence(
            proposal_id=proposal_id,
            validation_report_id=validation_id,
            evaluation_report_digest=str(fixed_set_result_json["report_digest"]),
            policy_snapshot_digest=str(latency_cost_json["snapshot_digest"]),
            source_evaluation_ids=source_evaluation_ids,
            source_trajectory_ids=source_trajectory_ids,
        )

    def _load_evaluation_cases(
        self,
        *,
        binding: ReleaseBindingV1,
        trajectory_ids: Sequence[str],
    ) -> tuple[dict[str, Any], ...]:
        unique_ids = tuple(sorted(set(trajectory_ids)))
        if len(unique_ids) != len(REGISTERED_FIXED_CASE_IDS):
            raise ValueError(
                "validation requires one persisted trajectory per registered fixed case"
            )
        cases: list[dict[str, Any]] = []
        for trajectory_id in unique_ids:
            row = self._connection.execute(
                """
                SELECT tt.status, tt.task_family, tt.agent_version,
                       tt.knowledge_version, tt.environment_version,
                       tt.evidence_refs_json,
                       te.result_json, te.process_json, te.quality_json,
                       te.failure_tags_json, te.confidence, te.learning_eligible
                FROM task_trajectories tt
                JOIN task_evaluations te ON te.trajectory_id = tt.id
                WHERE tt.id = ?
                """,
                (trajectory_id,),
            ).fetchone()
            if row is None:
                raise ValueError("validation trajectory is not persisted")
            if row["status"] != "active" or int(
                row["learning_eligible"]
            ) != 1:
                raise ValueError("validation trajectory is not eligible")
            process = _json_obj(row["process_json"])
            set_name = str(process.get("set_name", ""))
            case_id = str(process.get("case_id", ""))
            if set_name not in _FIXED_EVAL_SETS or not case_id:
                raise ValueError("validation trajectory fixed-set identity missing")
            assert_registered_fixed_case(case_id, set_name)
            if row["task_family"] != binding.target_component:
                raise ValueError("validation trajectory target component mismatch")
            if _json_list(row["failure_tags_json"]):
                raise ValueError("validation trajectory failure tags block promotion")
            self._verify_trajectory_integrity(dict(row), trajectory_id=trajectory_id)
            cases.append(
                {
                    "case_id": case_id,
                    "set_name": set_name,
                    "trajectory_id": trajectory_id,
                    "evidence": {
                        "result": [f"trajectory:{trajectory_id}:result"],
                        "process": [f"trajectory:{trajectory_id}:process"],
                        "quality": [f"trajectory:{trajectory_id}:quality"],
                    },
                    "result": _json_obj(row["result_json"]),
                    "process": process,
                    "quality": _json_obj(row["quality_json"]),
                    "failure_tags": _json_list(row["failure_tags_json"]),
                }
            )
        assert_required_fixed_case_coverage(str(case["case_id"]) for case in cases)
        if {case["set_name"] for case in cases} != set(REGISTERED_FIXED_SET_NAMES):
            raise ValueError("validation trajectories must cover the four fixed sets")
        return tuple(cases)

    def _insert_proposal_state_event(
        self,
        *,
        proposal_id: str,
        previous_state: str | None,
        next_state: str,
        actor_role: EvolutionRole,
        actor_id: str,
        binding_digest: str,
        event_json: dict[str, Any],
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO proposal_state_events (
              id, proposal_id, previous_state, next_state, actor_role, actor_id,
              binding_digest, event_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                proposal_id,
                previous_state or "",
                next_state,
                actor_role.value,
                actor_id,
                binding_digest,
                _json_text(event_json),
            ),
        )

    def _transition_proposal(
        self,
        *,
        proposal_id: str,
        previous_state: str,
        next_state: str,
        actor_role: EvolutionRole,
        actor_id: str,
        binding_digest: str,
        event_json: dict[str, Any],
    ) -> None:
        cursor = self._connection.execute(
            """
            UPDATE evolution_proposals
            SET state = ?
            WHERE id = ? AND state = ?
            """,
            (next_state, proposal_id, previous_state),
        )
        if cursor.rowcount != 1:
            raise ValueError("proposal state changed before validation could be recorded")
        self._insert_proposal_state_event(
            proposal_id=proposal_id,
            previous_state=previous_state,
            next_state=next_state,
            actor_role=actor_role,
            actor_id=actor_id,
            binding_digest=binding_digest,
            event_json=event_json,
        )

    def _verify_trajectory_integrity(
        self, row: Mapping[str, Any], *, trajectory_id: str
    ) -> None:
        evidence_refs = _json_obj(row["evidence_refs_json"])
        created_at = str(evidence_refs.get("created_at", ""))
        events = evidence_refs.get("events", [])
        envelope = TrajectoryEnvelopeV1.from_mapping(
            {
                "trajectory_id": trajectory_id,
                "task_id": evidence_refs.get("task_id", trajectory_id),
                "task_family": row["task_family"],
                "agent_version": row["agent_version"],
                "knowledge_version": row["knowledge_version"],
                "environment_version": row["environment_version"],
                "created_at": created_at,
                "result": _json_obj(row["result_json"]),
                "process": _json_obj(row["process_json"]),
                "quality": _json_obj(row["quality_json"]),
                "failure_tags": _json_list(row["failure_tags_json"]),
                "confidence": float(row["confidence"]),
                "user_feedback": evidence_refs.get("user_feedback"),
                "learning_eligible": bool(row["learning_eligible"]),
                "evidence_state": str(row["status"]),
                "events": events,
            }
        )
        if evidence_refs.get("request_digest") != envelope.canonical_digest():
            raise ValueError("trajectory request digest mismatch")
        expected_hmac = envelope.deployment_hmac_digest(self._deployment_secret)
        if not hmac.compare_digest(
            str(evidence_refs.get("deployment_hmac_digest", "")), expected_hmac
        ):
            raise ValueError("trajectory deployment HMAC mismatch")
        if evidence_refs.get("event_chain_digest") != envelope.event_chain_digest:
            raise ValueError("trajectory event chain digest mismatch")
        if events != [event.canonical_payload() for event in envelope.events]:
            raise ValueError("trajectory stored event chain mismatch")
        if evidence_refs.get("event_hashes") != [
            event.event_hash for event in envelope.events
        ]:
            raise ValueError("trajectory event hash mismatch")
        if evidence_refs.get("canary_observation") != envelope.process.get(
            "canary_observation"
        ):
            raise ValueError("trajectory canary assignment mismatch")

    def record_release_review_evidence(
        self,
        *,
        binding: ReleaseBindingV1,
        proposal_id: str,
        proposer_id: str,
        reviewer_decision_ref: str,
        reviewer_context: EvolutionCommandContext,
    ) -> ReleaseReviewEvidence:
        self._state_machine.validate_command_context(
            reviewer_context,
            required_role=EvolutionRole.REVIEWER,
            required_capability=EvolutionCapability.REVIEW,
        )
        self._validate_binding(binding)
        if reviewer_decision_ref != binding.reviewer_decision_ref:
            raise ValueError("reviewer decision ref must match release binding")
        self._validate_reviewers(
            proposer_id=proposer_id,
            reviewer_id=reviewer_context.actor_id,
        )
        proposal = self._connection.execute(
            """
            SELECT proposer_id, target_component, state
            FROM evolution_proposals
            WHERE id = ?
            """,
            (proposal_id,),
        ).fetchone()
        if proposal is None:
            raise ValueError("review requires persisted validation proposal")
        if proposal["proposer_id"] != proposer_id:
            raise ValueError("review proposer must match validation proposal")
        if proposal["target_component"] != binding.target_component:
            raise ValueError("review target component must match release binding")
        if proposal["state"] != "validating":
            raise ValueError("independent review requires completed validation")
        validations = self._connection.execute(
            "SELECT id, fixed_set_result_json FROM validation_reports "
            "WHERE proposal_id = ? AND status = 'approved'", (proposal_id,)
        ).fetchall()
        if len(validations) != 1:
            raise ValueError("review requires exactly one successful validation report")
        report = _json_obj(validations[0]["fixed_set_result_json"])
        _validate_canonical_promotion_report(report, binding=binding)
        self._validate_report_execution(report, proposal_id=proposal_id, binding=binding)
        artifact_payload = self.load_proposal_artifact(proposal_id, binding)
        review_artifact = {
            "schema_version": "review-artifact.v1",
            "proposal_id": proposal_id,
            "binding_digest": binding.canonical_digest(),
            "validation_report_digest": str(report["report_digest"]),
            "artifact_digest": binding.approved_artifact_digest,
            "reviewer_id": reviewer_context.actor_id,
            "decision": "approve",
            "decision_ref": reviewer_decision_ref,
        }
        review_artifact_digest = f"sha256:{sha256_json(review_artifact)}"
        review_id = new_id()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO review_reports (
                  id, proposal_id, reviewer_id, decision, rationale, evidence_refs_json
                )
                VALUES (?, ?, ?, 'approve', ?, ?)
                """,
                (
                    review_id,
                    proposal_id,
                    reviewer_context.actor_id,
                    reviewer_decision_ref,
                    _json_text({
                        "source_evidence_refs": list(binding.source_evidence_refs),
                        "review_artifact": review_artifact,
                        "review_artifact_digest": review_artifact_digest,
                    }),
                ),
            )
            self._ensure_approved_release_artifact(binding, artifact_payload=artifact_payload)
            self._transition_proposal(
                proposal_id=proposal_id,
                previous_state="validating",
                next_state="approved",
                actor_role=EvolutionRole.REVIEWER,
                actor_id=reviewer_context.actor_id,
                binding_digest=binding.canonical_digest(),
                event_json={
                    "validation_report_id": validations[0]["id"],
                    "review_report_id": review_id,
                    "evaluation_report_digest": report["report_digest"],
                },
            )
        return ReleaseReviewEvidence(review_report_id=review_id)

    def promote_release(
        self,
        release_id: str,
        *,
        user_approval_context: EvolutionCommandContext,
        publisher_context: EvolutionCommandContext,
        request_id: str,
    ) -> ReleaseContext:
        self._state_machine.validate_publish(
            publisher=publisher_context,
            user_approver=user_approval_context,
        )
        if self._connection.in_transaction:
            raise ValueError("promotion requires a transaction-free connection")
        self._preflight_canary_sample_count(release_id)
        release = self.load_release(release_id)
        if release.state is ReleaseState.STABLE:
            if self._event_request_seen(release_id, request_id):
                return release
            raise ValueError("release is already stable")
        if release.state is not ReleaseState.CANARY:
            raise ValueError("promotion requires a canary release")

        spec = self._prepared_spec(release_id)
        self._validate_binding(spec["binding"])
        self._validate_release_integrity(release)
        self._validate_reviewers(
            proposer_id=spec["proposer_id"], reviewer_id=spec["reviewer_id"]
        )
        self._validate_fixed_sets(spec["binding"])
        self._validate_canary_assignment(spec["canary_assignment"])
        self._validate_promotion_evidence(
            release_id=release_id,
            binding=spec["binding"],
            canary_samples=int(spec["canary_samples"]),
        )

        current_stable = self.load_default_head(release.target_component)
        if current_stable is None:
            raise ValueError("promotion requires a current stable head")
        if spec["binding"].rollback_target_id != current_stable.release_id:
            raise ValueError("rollback target must remain the current stable release")

        if self._event_request_seen(release_id, request_id):
            return self.load_release(release_id)

        with self._connection:
            # sqlite3's context manager alone does not begin a transaction for
            # SELECT. Lock before the final reads so no concurrent promotion
            # can change the stable head between validation and publication.
            self._connection.execute("BEGIN IMMEDIATE")
            fresh_release = self.load_release(release_id)
            fresh_stable = self.load_default_head(release.target_component)
            fresh_spec = self._prepared_spec(release_id)
            if (
                fresh_release.state is not ReleaseState.CANARY
                or fresh_stable is None
                or fresh_stable.release_id != current_stable.release_id
                or fresh_spec != spec
            ):
                raise ValueError("release or stable head changed before promotion")
            spec = fresh_spec
            self._validate_release_integrity(fresh_release)
            self._validate_canary_observations(
                release_id=release_id,
                binding=spec["binding"],
                canary_assignment=spec["canary_assignment"],
                min_samples=int(spec["canary_samples"]),
            )
            self._connection.execute(
                """
                UPDATE strategy_releases
                SET rollback_target_release_id = ?, activated_at = ?, updated_at = ?
                WHERE id = ? AND state = 'canary'
                """,
                (current_stable.release_id, _utc_now(), _utc_now(), release_id),
            )
            if self._connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("promotion activation update affected no canary release")
            self._advance_release(
                current_stable.release_id,
                previous_state=ReleaseState.STABLE,
                next_state=ReleaseState.ROLLED_BACK,
                actor_id=publisher_context.actor_id,
                request_id=request_id,
                step="rollback",
                binding=current_stable.binding,
            )
            self._advance_release(
                release_id,
                previous_state=ReleaseState.CANARY,
                next_state=ReleaseState.STABLE,
                actor_id=publisher_context.actor_id,
                request_id=request_id,
                step="stable",
                binding=spec["binding"],
                canary_assignment=spec["canary_assignment"],
                rollback_target_release_id=current_stable.release_id,
                event_metadata={"user_approver_id": user_approval_context.actor_id},
            )
        return self.load_release(release_id)

    def advance_release_stage(
        self,
        release_id: str,
        *,
        next_state: ReleaseState,
        publisher_context: EvolutionCommandContext,
        stage_evidence_ids: Sequence[str] = (),
        execution_run_id: str | None = None,
        request_id: str,
        step: str,
    ) -> ReleaseContext:
        if self._connection.in_transaction:
            raise ValueError("stage advancement requires a transaction-free connection")
        release = self.load_release(release_id)
        spec = self._prepared_spec(release_id)
        self._state_machine.validate_command_context(
            publisher_context,
            required_role=EvolutionRole.PUBLISHER,
            required_capability=EvolutionCapability.PUBLISH,
        )
        self._validate_release_integrity(release)
        self._validate_promotion_evidence(
            release_id=release_id,
            binding=spec["binding"],
            canary_samples=int(spec["canary_samples"]),
        )
        previous_state = release.state
        if previous_state is next_state and self._event_request_seen(release_id, request_id):
            return release
        allowed_stage = {
            ReleaseState.PREPARED: ReleaseState.REPLAY,
            ReleaseState.REPLAY: ReleaseState.SHADOW,
            ReleaseState.SHADOW: ReleaseState.CANARY,
        }.get(previous_state)
        if next_state is not allowed_stage:
            raise ValueError(
                "generic stage advancement is limited to replay, shadow, and canary; "
                "stable and rollback require their dedicated approved commands"
            )
        execution = self._validate_stage_execution(
            release, execution_run_id=execution_run_id, expected_stage=next_state.value,
        )
        executed_ids = tuple(str(item) for item in execution["trajectory_ids"])
        if stage_evidence_ids and sorted(stage_evidence_ids) != sorted(executed_ids):
            raise ValueError("stage trajectories must match protected stage execution")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            fresh = self.load_release(release_id)
            if fresh.state != previous_state or self._prepared_spec(release_id) != spec:
                raise ValueError("release changed before stage advancement")
            self._validate_release_integrity(fresh)
            self._validate_stage_execution(
                fresh, execution_run_id=execution_run_id, expected_stage=next_state.value,
            )
            self._advance_release(
                release_id,
                previous_state=previous_state,
                next_state=next_state,
                actor_id=publisher_context.actor_id,
                request_id=request_id,
                step=step,
                binding=spec["binding"],
                canary_assignment=spec["canary_assignment"],
                canary_samples=int(spec["canary_samples"])
                if next_state is ReleaseState.CANARY
                else None,
                event_metadata={
                    "stage_evidence_ids": list(executed_ids),
                    "execution_run_id": execution_run_id,
                    "execution_record_digest": _digest_mapping(execution),
                },
            )
        return self.load_release(release_id)

    def source_trajectory_ids(self, release_id: str) -> tuple[str, ...]:
        row = self._connection.execute(
            """
            SELECT ri.source_trajectory_ids_json
            FROM strategy_releases sr
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            WHERE sr.id = ?
            """,
            (release_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown release: {release_id}")
        payload = _json_any(row["source_trajectory_ids_json"])
        if not isinstance(payload, dict):
            raise ValueError("release source trajectory binding missing")
        return tuple(str(item) for item in payload.get("source_trajectory_ids", ()))

    def stage_evidence_ids(
        self,
        release_id: str,
        *,
        next_state: ReleaseState,
    ) -> tuple[str, ...]:
        row = self._connection.execute(
            """
            SELECT event_json FROM release_transition_events
            WHERE release_id = ? AND next_state = ?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (release_id, next_state.value),
        ).fetchone()
        if row is None:
            raise ValueError("release stage evidence event missing")
        payload = _json_obj(row["event_json"])
        ids = tuple(
            str(item) for item in _json_list(payload.get("stage_evidence_ids", []))
        )
        if not ids:
            raise ValueError("release stage evidence ids missing")
        return ids

    def rollback_release(
        self,
        release_id: str,
        *,
        user_approval_context: EvolutionCommandContext,
        publisher_context: EvolutionCommandContext,
        request_id: str,
    ) -> ReleaseContext:
        self._state_machine.validate_publish(
            publisher=publisher_context,
            user_approver=user_approval_context,
        )
        release = self.load_release(release_id)
        if release.rollback_target_id is None:
            raise ValueError("rollback target missing")
        if release.rollback_target_id == release.release_id:
            raise ValueError("baseline release has no earlier rollback target")

        target = self.load_release(release.rollback_target_id)
        # A rollback is a privileged restore of immutable release state.  Verify
        # both sides (binding, artifact, input digest and rollback binding) before
        # mutating either head so a tampered target cannot become the default.
        self._validate_release_integrity(release)
        self._validate_release_integrity(target)
        if release.target_component != target.target_component:
            raise ValueError("rollback target component mismatch")
        if release.binding.rollback_target_id != target.release_id:
            raise ValueError("rollback target binding mismatch")
        if release.state is ReleaseState.ROLLED_BACK:
            if self._event_request_seen(release_id, request_id):
                return target
            raise ValueError("release is already rolled back")
        if release.state is not ReleaseState.STABLE:
            raise ValueError("rollback requires a stable release")
        if target.state is not ReleaseState.ROLLED_BACK and target.state is not ReleaseState.STABLE:
            raise ValueError("rollback target is not recoverable")
        current_head = self.load_default_head(release.target_component)
        if current_head is None or current_head.release_id != release.release_id:
            raise ValueError("rollback target must be the current stable release")
        if self._event_request_seen(release_id, request_id):
            return self.load_release(target.release_id)

        with self._connection:
            self._advance_release(
                release_id,
                previous_state=ReleaseState.STABLE,
                next_state=ReleaseState.ROLLED_BACK,
                actor_id=publisher_context.actor_id,
                request_id=request_id,
                step="rollback",
                binding=release.binding,
                rollback_target_release_id=release.rollback_target_id,
                event_metadata={"user_approver_id": user_approval_context.actor_id},
            )
            self._advance_release(
                target.release_id,
                previous_state=ReleaseState.ROLLED_BACK,
                next_state=ReleaseState.STABLE,
                actor_id=publisher_context.actor_id,
                request_id=request_id,
                step="restore",
                binding=target.binding,
                rollback_target_release_id=target.binding.rollback_target_id,
                event_metadata={
                    "user_approver_id": user_approval_context.actor_id,
                    "restored_from_release_id": release_id,
                },
            )
        return self.load_release(target.release_id)

    def _create_release(
        self,
        *,
        binding: ReleaseBindingV1,
        proposal_id: str,
        validation_report_id: str,
        review_report_id: str,
        release_gate: dict[str, Any],
        source_evaluation_ids: tuple[str, ...],
        source_trajectory_ids: tuple[str, ...],
        proposer_id: str,
        reviewer_id: str,
        canary_assignment: CanaryAssignment,
        canary_samples: int,
        request_id: str,
        initial_state: ReleaseState,
    ) -> ReleaseContext:
        release_id = new_id()
        release_input_id = new_id()
        now = _utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO release_inputs (
                  id, release_input_id, target_component, proposal_id, source_trajectory_ids_json,
                  validation_report_id, review_report_id, risk_policy_snapshot_id,
                  rollback_target_release_id, input_sha256, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release_input_id,
                    request_id,
                    binding.target_component,
                    proposal_id,
                    _json_text(
                        {
                            "release_gate": release_gate,
                            "source_evaluation_ids": list(source_evaluation_ids),
                            "source_trajectory_ids": list(source_trajectory_ids),
                        }
                    ),
                    validation_report_id,
                    review_report_id,
                    new_id(),
                    binding.rollback_target_id,
                    _release_input_digest(
                        binding=binding,
                        release_gate=release_gate,
                        source_evaluation_ids=source_evaluation_ids,
                        source_trajectory_ids=source_trajectory_ids,
                    ),
                    now,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO strategy_releases (
                  id, release_input_id, target_component, state, risk_level,
                  canary_scope_json, rollback_target_release_id, activated_at,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'low', ?, ?, NULL, ?, ?)
                """,
                (
                    release_id,
                    release_input_id,
                    binding.target_component,
                    initial_state.value,
                    _json_text(canary_assignment.canonical_payload()),
                    binding.rollback_target_id,
                    now,
                    now,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO strategy_release_heads (
                  release_id, target_component, binding_digest, release_state, head_event_id,
                  approved_artifact_digest, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release_id,
                    binding.target_component,
                    binding.canonical_digest(),
                    initial_state.value,
                    new_id(),
                    binding.approved_artifact_digest,
                    now,
                    now,
                ),
            )
            self._insert_transition_event(
                release_id=release_id,
                previous_state=ReleaseState.PREPARED,
                next_state=initial_state,
                actor_role=EvolutionRole.REVIEWER,
                actor_id=reviewer_id,
                request_id=request_id,
                binding=binding,
                canary_assignment=canary_assignment,
                canary_samples=canary_samples,
                event_metadata={
                    "proposer_id": proposer_id,
                    "reviewer_id": reviewer_id,
                    "evaluation_report_digest": release_gate["evaluation_report_digest"],
                    "policy_snapshot_digest": release_gate["policy_snapshot_digest"],
                },
                step="prepared" if initial_state is ReleaseState.PREPARED else "bootstrap",
            )
        return self.load_release(release_id)


    def _advance_release(
        self,
        release_id: str,
        *,
        previous_state: ReleaseState,
        next_state: ReleaseState,
        actor_id: str,
        request_id: str,
        step: str,
        binding: ReleaseBindingV1,
        canary_assignment: CanaryAssignment | None = None,
        canary_samples: int | None = None,
        rollback_target_release_id: str | None = None,
        event_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._state_machine.validate_release_transition(previous_state, next_state)
        cursor = self._connection.execute(
            """
            UPDATE strategy_releases
            SET state = ?, canary_scope_json = COALESCE(canary_scope_json, ?), updated_at = ?
            WHERE id = ? AND state = ?
            """,
            (
                next_state.value,
                _json_text(canary_assignment.canonical_payload()) if canary_assignment else None,
                _utc_now(),
                release_id,
                previous_state.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("release state changed before transition could be recorded")
        self._connection.execute(
            """
            UPDATE strategy_release_heads
            SET release_state = ?, head_event_id = ?, updated_at = ?
            WHERE release_id = ?
            """,
            (next_state.value, new_id(), _utc_now(), release_id),
        )
        if next_state is ReleaseState.CANARY and canary_samples is not None:
            self._connection.execute(
                """
                INSERT INTO canary_assignments (
                  id, release_id, cohort_key, assignment_state, binding_digest,
                  sample_size, created_at, updated_at
                )
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    release_id,
                    _json_text(canary_assignment.canonical_payload()) if canary_assignment else "",
                    binding.canonical_digest(),
                    canary_samples,
                    _utc_now(),
                    _utc_now(),
                ),
            )
        self._insert_transition_event(
            release_id=release_id,
            previous_state=previous_state,
            next_state=next_state,
            actor_role=EvolutionRole.PUBLISHER,
            actor_id=actor_id,
            request_id=request_id,
            binding=binding,
            canary_assignment=canary_assignment,
            canary_samples=canary_samples,
            rollback_target_release_id=rollback_target_release_id,
            event_metadata=event_metadata,
            step=step,
        )

    def _insert_transition_event(
        self,
        *,
        release_id: str,
        previous_state: ReleaseState,
        next_state: ReleaseState,
        actor_role: EvolutionRole,
        actor_id: str,
        request_id: str,
        binding: ReleaseBindingV1,
        step: str,
        canary_assignment: CanaryAssignment | None = None,
        canary_samples: int | None = None,
        rollback_target_release_id: str | None = None,
        event_metadata: dict[str, Any] | None = None,
    ) -> str:
        event_id = new_id()
        event_json: dict[str, Any] = {
            "binding": binding.as_record(),
            "request_id": request_id,
            "step": step,
        }
        if event_metadata is not None:
            event_json.update(event_metadata)
        if canary_assignment is not None:
            event_json["canary_assignment"] = canary_assignment.canonical_payload()
        if canary_samples is not None:
            event_json["canary_samples"] = canary_samples
        if rollback_target_release_id is not None:
            event_json["rollback_target_release_id"] = rollback_target_release_id
        self._connection.execute(
            """
            INSERT INTO release_transition_events (
              id, release_id, previous_state, next_state, actor_role, actor_id, binding_digest,
              reason, event_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                release_id,
                previous_state.value,
                next_state.value,
                actor_role.value,
                actor_id,
                binding.canonical_digest(),
                step,
                _json_text(event_json),
                _utc_now(),
                _utc_now(),
            ),
        )
        return event_id

    def _load_context(self, row: sqlite3.Row) -> ReleaseContext:
        binding = self._load_binding(row["id"])
        canary_assignment = None
        if row["canary_scope_json"]:
            canary_assignment = CanaryAssignment(
                scope=cast(dict[str, Any], _json_obj(row["canary_scope_json"])["scope"]),
                expires_at=str(_json_obj(row["canary_scope_json"])["expires_at"]),
            )
        return ReleaseContext(
            release_id=row["id"],
            target_component=row["target_component"],
            state=ReleaseState(row["state"]),
            binding=binding,
            binding_digest=binding.canonical_digest(),
            rollback_target_id=row["rollback_target_release_id"],
            canary_assignment=canary_assignment,
            transition_audit=self.load_history(row["id"]),
        )

    def _load_binding(self, release_id: str) -> ReleaseBindingV1:
        row = self._connection.execute(
            """
            SELECT event_json
            FROM release_transition_events
            WHERE release_id = ?
            ORDER BY created_at, id
            LIMIT 1
            """,
            (release_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"release binding not found for {release_id}")
        payload = _json_obj(cast(sqlite3.Row, row)["event_json"])["binding"]
        return ReleaseBindingV1(
            candidate_id=str(payload["candidate_id"]),
            target_component=str(payload["target_component"]),
            source_evaluation_ids=tuple(str(item) for item in payload["source_evaluation_ids"]),
            source_evidence_refs=tuple(str(item) for item in payload["source_evidence_refs"]),
            validation_report_ref=str(payload["validation_report_ref"]),
            reviewer_decision_ref=str(payload["reviewer_decision_ref"]),
            approved_artifact_digest=str(payload["approved_artifact_digest"]),
            rollback_target_id=str(payload["rollback_target_id"]),
        )

    def _prepared_spec(self, release_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            """
            SELECT event_json
            FROM release_transition_events
            WHERE release_id = ? AND next_state = 'prepared'
            ORDER BY created_at, id
            LIMIT 1
            """,
            (release_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"prepared spec not found for {release_id}")
        payload = _json_obj(cast(sqlite3.Row, row)["event_json"])
        return {
            "binding": ReleaseBindingV1(
                candidate_id=str(payload["binding"]["candidate_id"]),
                target_component=str(payload["binding"]["target_component"]),
                source_evaluation_ids=tuple(
                    str(item) for item in payload["binding"]["source_evaluation_ids"]
                ),
                source_evidence_refs=tuple(
                    str(item) for item in payload["binding"]["source_evidence_refs"]
                ),
                validation_report_ref=str(payload["binding"]["validation_report_ref"]),
                reviewer_decision_ref=str(payload["binding"]["reviewer_decision_ref"]),
                approved_artifact_digest=str(payload["binding"]["approved_artifact_digest"]),
                rollback_target_id=str(payload["binding"]["rollback_target_id"]),
            ),
            "proposer_id": str(payload["proposer_id"]),
            "reviewer_id": str(payload["reviewer_id"]),
            "canary_assignment": CanaryAssignment(
                scope=cast(dict[str, Any], payload["canary_assignment"]["scope"]),
                expires_at=str(payload["canary_assignment"]["expires_at"]),
            ),
            "canary_samples": int(payload["canary_samples"]),
        }

    def _find_release_by_request(self, request_id: str) -> ReleaseContext | None:
        row = self._connection.execute(
            """
            SELECT sr.*
            FROM strategy_releases sr
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            WHERE ri.release_input_id = ?
            ORDER BY sr.created_at DESC
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return self._load_context(cast(sqlite3.Row, row))

    def _event_request_seen(self, release_id: str, request_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM release_transition_events
            WHERE release_id = ? AND json_extract(event_json, '$.request_id') = ?
            LIMIT 1
            """,
            (release_id, request_id),
        ).fetchone()
        return row is not None

    def _validate_binding(self, binding: ReleaseBindingV1) -> None:
        if set(binding.source_evaluation_ids) != set(REGISTERED_FIXED_SET_NAMES):
            raise ValueError("release must reference the four fixed eval sets")
        if len(binding.source_evaluation_ids) != len(REGISTERED_FIXED_SET_NAMES):
            raise ValueError("release must reference the four fixed eval sets")
        if binding.target_component != "retrieval.answer_strategy":
            raise ValueError("unexpected target component")

    def _validate_reviewers(self, *, proposer_id: str, reviewer_id: str) -> None:
        self._state_machine.validate_review_assignment(
            proposer=command_context_for_role(proposer_id, EvolutionRole.PROPOSER),
            reviewer=command_context_for_role(reviewer_id, EvolutionRole.REVIEWER),
        )

    def _validate_fixed_sets(self, binding: ReleaseBindingV1) -> None:
        if set(binding.source_evaluation_ids) != set(REGISTERED_FIXED_SET_NAMES):
            raise ValueError("release must reference the four fixed eval sets")

    def _validate_canary_assignment(self, assignment: CanaryAssignment) -> None:
        if not assignment.scope:
            raise ValueError("canary assignment requires scope")
        if not assignment.expires_at:
            raise ValueError("canary assignment requires expiry")

    def _validate_canary_samples(self, canary_samples: int) -> None:
        if canary_samples < _CANARY_MIN_SAMPLES:
            raise ValueError("canary requires at least five samples")

    def _validate_release_integrity(self, release: ReleaseContext) -> None:
        row = self._connection.execute(
            """
            SELECT srh.binding_digest, srh.approved_artifact_digest, ri.input_sha256,
                   srh.target_component, srh.release_state,
                   ri.rollback_target_release_id, ri.source_trajectory_ids_json,
                   vr.proposal_id, vr.fixed_set_result_json
            FROM strategy_release_heads srh
            JOIN strategy_releases sr ON sr.id = srh.release_id
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            JOIN validation_reports vr ON vr.id = ri.validation_report_id
            WHERE srh.release_id = ?
            """,
            (release.release_id,),
        ).fetchone()
        if row is None:
            raise ValueError("release integrity metadata missing")
        if (
            row["target_component"] != release.target_component
            or release.target_component != release.binding.target_component
        ):
            raise ValueError("release target component mismatch")
        if row["release_state"] != release.state.value:
            raise ValueError("release head state mismatch")
        binding_digest = release.binding.canonical_digest()
        if row["binding_digest"] != binding_digest:
            raise ValueError("release binding digest mismatch")
        release_input = _json_any(row["source_trajectory_ids_json"])
        if isinstance(release_input, dict) and "release_gate" in release_input:
            if row["input_sha256"] != _release_input_digest(
                binding=release.binding,
                source_trajectory_ids=tuple(
                    str(item) for item in release_input.get("source_trajectory_ids", [])
                ),
                source_evaluation_ids=tuple(
                    str(item) for item in release_input.get("source_evaluation_ids", [])
                ),
                release_gate=_json_mapping(release_input["release_gate"]),
            ):
                raise ValueError("release input digest mismatch")
        elif row["input_sha256"] != binding_digest:
            raise ValueError("release input digest mismatch")
        if row["approved_artifact_digest"] != release.binding.approved_artifact_digest:
            raise ValueError("approved artifact digest mismatch")
        self._load_verified_release_artifact(
            binding_digest=binding_digest,
            artifact_digest=release.binding.approved_artifact_digest,
        )
        baseline_without_target = (
            release.release_id == "00000000-0000-4000-8000-000000000501"
            and row["rollback_target_release_id"] is None
        )
        if not baseline_without_target:
            self._validate_report_execution(
                _json_obj(row["fixed_set_result_json"]),
                proposal_id=str(row["proposal_id"]), binding=release.binding,
            )
            self._validate_stage_history(release)
        if (
            not baseline_without_target
            and row["rollback_target_release_id"] != release.binding.rollback_target_id
        ):
            raise ValueError("rollback target digest binding mismatch")
        if (
            not baseline_without_target
            and release.rollback_target_id != release.binding.rollback_target_id
        ):
            raise ValueError("rollback target binding mismatch")

    def _validate_promotion_evidence(
        self,
        *,
        release_id: str,
        binding: ReleaseBindingV1,
        canary_samples: int,
    ) -> None:
        row = self._connection.execute(
            """
            SELECT vr.fixed_set_result_json, vr.dynamic_set_result_json, vr.latency_cost_json,
                   vr.status, vr.proposal_id, rr.decision, rr.reviewer_id,
                   ri.source_trajectory_ids_json, rr.evidence_refs_json
            FROM strategy_releases sr
            JOIN release_inputs ri ON ri.id = sr.release_input_id
            JOIN validation_reports vr ON vr.id = ri.validation_report_id
            JOIN review_reports rr ON rr.id = ri.review_report_id
            WHERE sr.id = ?
            """,
            (release_id,),
        ).fetchone()
        if row is None:
            raise ValueError("release validation evidence missing")
        if row["status"] != "approved" or row["decision"] != "approve":
            raise ValueError("release validation and review must be approved")
        review_refs = _json_obj(row["evidence_refs_json"])
        review_artifact = _json_obj(review_refs.get("review_artifact"))
        review_digest = review_refs.get("review_artifact_digest")
        if (
            not review_artifact
            or not isinstance(review_digest, str)
            or review_digest != f"sha256:{sha256_json(review_artifact)}"
            or review_artifact.get("binding_digest") != binding.canonical_digest()
            or review_artifact.get("artifact_digest") != binding.approved_artifact_digest
            or review_artifact.get("decision") != "approve"
        ):
            raise ValueError("review artifact is missing or does not match release input")
        report = _json_obj(row["fixed_set_result_json"])
        dynamic = _json_obj(row["dynamic_set_result_json"])
        policy = _json_obj(row["latency_cost_json"])
        release_input = _json_any(row["source_trajectory_ids_json"])
        _validate_canonical_promotion_report(report, binding=binding)
        self._validate_report_execution(
            report, proposal_id=str(row["proposal_id"]), binding=binding,
        )
        _validate_dynamic_report(dynamic)
        _validate_policy_snapshot(policy, canary_samples=canary_samples)
        release_gate = _release_gate_from_input(release_input)
        if release_gate is None:
            raise ValueError("release input promotion gate missing")
        if release_gate.get("promotion_eligible") is not True:
            raise ValueError("release input promotion eligibility must be persisted")
        if release_gate.get("evaluation_report_digest") != report.get("report_digest"):
            raise ValueError("release input evaluation report digest mismatch")
        if release_gate.get("policy_snapshot_digest") != policy.get("snapshot_digest"):
            raise ValueError("release input policy snapshot digest mismatch")
        self._load_verified_release_artifact(
            binding_digest=binding.canonical_digest(),
            artifact_digest=binding.approved_artifact_digest,
        )

    def _validate_report_execution(
        self, report: dict[str, Any], *, proposal_id: str, binding: ReleaseBindingV1,
    ) -> None:
        run_id = report.get("execution_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("validation report requires protected execution reference")
        row = self._connection.execute(
            "SELECT * FROM proposal_execution_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("validation execution reference not found")
        execution = verify_execution_record(dict(row), secret=self._deployment_secret)
        require_passing_execution(
            execution, proposal_id=proposal_id, binding_digest=binding.canonical_digest(),
            artifact_digest=binding.approved_artifact_digest,
        )
        if report.get("execution_record_digest") != _digest_mapping(execution):
            raise ValueError("validation execution digest mismatch")
        cases = self._load_evaluation_cases(
            binding=binding, trajectory_ids=execution.get("trajectory_ids", []),
        )
        self._validate_execution_case_links(execution, cases)

    def _stage_execution_id(self, release_id: str, next_state: ReleaseState) -> str:
        row = self._connection.execute(
            "SELECT event_json FROM release_transition_events "
            "WHERE release_id = ? AND next_state = ? ORDER BY created_at, id LIMIT 1",
            (release_id, next_state.value),
        ).fetchone()
        value = _json_obj(row["event_json"]).get("execution_run_id") if row else None
        if not isinstance(value, str) or not value:
            raise ValueError("release stage requires a protected execution reference")
        return value

    def _validate_stage_history(self, release: ReleaseContext) -> None:
        required = {
            ReleaseState.REPLAY: (ReleaseState.REPLAY,),
            ReleaseState.SHADOW: (ReleaseState.REPLAY, ReleaseState.SHADOW),
            ReleaseState.CANARY: (ReleaseState.REPLAY, ReleaseState.SHADOW, ReleaseState.CANARY),
            ReleaseState.STABLE: (ReleaseState.REPLAY, ReleaseState.SHADOW, ReleaseState.CANARY),
            ReleaseState.ROLLED_BACK: (
                ReleaseState.REPLAY, ReleaseState.SHADOW, ReleaseState.CANARY,
            ),
        }.get(release.state, ())
        for stage in required:
            run_id = self._stage_execution_id(release.release_id, stage)
            execution = self._validate_stage_execution(
                release, execution_run_id=run_id,
                expected_stage=stage.value,
            )
            row = self._connection.execute(
                "SELECT event_json FROM release_transition_events "
                "WHERE release_id = ? AND next_state = ? ORDER BY created_at, id LIMIT 1",
                (release.release_id, stage.value),
            ).fetchone()
            payload = _json_obj(row["event_json"])
            if (
                payload.get("execution_record_digest") != _digest_mapping(execution)
                or payload.get("stage_evidence_ids") != execution.get("trajectory_ids")
            ):
                raise ValueError("stage transition execution digest mismatch")

    def _validate_stage_execution(
        self, release: ReleaseContext, *, execution_run_id: str | None, expected_stage: str,
    ) -> dict[str, Any]:
        if not execution_run_id:
            raise ValueError("release stage requires a protected execution run")
        row = self._connection.execute(
            "SELECT * FROM release_execution_runs WHERE id = ?", (execution_run_id,),
        ).fetchone()
        if row is None:
            raise ValueError("protected release execution run not found")
        record = verify_execution_record(dict(row), secret=self._deployment_secret)
        proposal = self._connection.execute(
            "SELECT ri.proposal_id FROM strategy_releases sr "
            "JOIN release_inputs ri ON ri.id = sr.release_input_id WHERE sr.id = ?",
            (release.release_id,),
        ).fetchone()
        if (
            proposal is None or record.get("release_id") != release.release_id
            or row["release_id"] != release.release_id
            or row["stage"] != expected_stage
            or record.get("release_state") != {
                "replay": "prepared", "shadow": "replay", "canary": "shadow",
            }.get(expected_stage)
            or record.get("binding") != release.binding.canonical_payload()
        ):
            raise ValueError("stage execution release binding mismatch")
        require_passing_execution(
            record, proposal_id=str(proposal["proposal_id"]),
            binding_digest=release.binding_digest,
            artifact_digest=release.binding.approved_artifact_digest,
            expected_stage=expected_stage,
        )
        baseline = self.load_release(release.binding.rollback_target_id)
        if record.get("baseline_binding_digest") != baseline.binding_digest:
            raise ValueError("stage execution baseline binding mismatch")
        cases = self._load_evaluation_cases(
            binding=release.binding, trajectory_ids=record.get("trajectory_ids", []),
        )
        self._validate_execution_case_links(record, cases)
        if any(
            case["process"].get("stage") != expected_stage
            or case["process"].get("release_id") != release.release_id
            for case in cases
        ):
            raise ValueError("trajectory execution stage mismatch")
        if expected_stage == "shadow":
            shadow = next(
                case["observed_facts"] for case in record["cases"]
                if case["case_id"] == "migration-answer-strategy-transfer-001"
            )
            if (
                shadow.get("same_database_snapshot_digest") is not True
                or shadow.get("source_snapshot_unchanged") is not True
                or shadow.get("candidate_artifact_digest")
                != release.binding.approved_artifact_digest
                or shadow.get("baseline_artifact_digest")
                != artifact_digest(self.load_release_artifact(baseline.release_id))
                or not shadow.get("source_snapshot_digest")
                or not shadow.get("baseline", {}).get("snapshot_digest")
                or not shadow.get("baseline", {}).get("input_digest")
                or shadow.get("source_snapshot_digest_after")
                != shadow.get("source_snapshot_digest")
                or shadow.get("baseline", {}).get("snapshot_digest")
                != shadow.get("candidate", {}).get("snapshot_digest")
                or shadow.get("baseline", {}).get("input_digest")
                != shadow.get("candidate", {}).get("input_digest")
            ):
                raise ValueError("shadow requires a protected same-snapshot paired execution")
            for side in ("baseline", "candidate"):
                facts = dict(shadow[side])
                digest = facts.pop("observation_digest", None)
                if digest != sha256_json({"facts": facts, "outcomes": shadow[f"{side}_outcomes"]}):
                    raise ValueError("shadow paired observation digest mismatch")
        return record

    @staticmethod
    def _validate_execution_case_links(
        execution: dict[str, Any], cases: Sequence[dict[str, Any]],
    ) -> None:
        observed = {case["case_id"]: case for case in execution["cases"]}
        for case in cases:
            expected = observed[case["case_id"]]
            process = case["process"]
            if (process.get("artifact_digest") != execution["artifact_digest"]
                or process.get("observation_digest") != expected["observation_digest"]
                or process.get("assertion_outcomes") != {
                    item["name"]: item["passed"] for item in expected["assertions"]
                }):
                raise ValueError("trajectory observations differ from protected execution")

    def _load_verified_release_artifact(
        self,
        *,
        binding_digest: str,
        artifact_digest: str,
    ) -> dict[str, Any]:
        row = self._connection.execute(
            """
            SELECT artifact_json
            FROM evolution_artifacts
            WHERE binding_digest = ?
              AND artifact_digest = ?
              AND artifact_kind = 'retrieval_strategy'
              AND status IN ('approved', 'published')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (binding_digest, artifact_digest),
        ).fetchone()
        if row is None:
            raise ValueError("approved release artifact missing")
        artifact = parse_artifact_json(cast(sqlite3.Row, row)["artifact_json"])
        if not isinstance(artifact, dict):
            raise ValueError("release artifact must be a JSON object")
        validate_artifact_digest(artifact, artifact_digest)
        validate_serving_strategy_artifact(artifact)
        return cast(dict[str, Any], artifact)

    def _ensure_approved_release_artifact(
        self,
        binding: ReleaseBindingV1,
        *,
        artifact_payload: dict[str, Any] | None,
    ) -> None:
        binding_digest = binding.canonical_digest()
        if artifact_payload is None:
            existing = self._connection.execute(
                """
                SELECT artifact_json
                FROM evolution_artifacts
                WHERE binding_digest = ?
                  AND artifact_digest = ?
                  AND artifact_kind = 'retrieval_strategy'
                  AND status IN ('approved', 'published')
                LIMIT 1
                """,
                (binding_digest, binding.approved_artifact_digest),
            ).fetchone()
            if existing is None:
                raise ValueError("approved release artifact must be persisted before review")
            parsed = parse_artifact_json(cast(sqlite3.Row, existing)["artifact_json"])
            if not isinstance(parsed, dict):
                raise ValueError("persisted release artifact must be a JSON object")
            validate_artifact_digest(parsed, binding.approved_artifact_digest)
            validate_strategy_artifact(parsed)
            return
        payload = artifact_payload
        digest = _validated_artifact_payload_digest(binding, payload)
        row = self._connection.execute(
            """
            SELECT artifact_json
            FROM evolution_artifacts
            WHERE binding_digest = ?
              AND artifact_digest = ?
              AND artifact_kind = 'retrieval_strategy'
              AND status IN ('approved', 'published')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (binding_digest, digest),
        ).fetchone()
        if row is not None:
            artifact = parse_artifact_json(cast(sqlite3.Row, row)["artifact_json"])
            if not isinstance(artifact, dict):
                raise ValueError("release artifact must be a JSON object")
            validate_artifact_digest(artifact, digest)
            return
        now = _utc_now()
        self._connection.execute(
            """
            INSERT INTO evolution_artifacts (
              id, artifact_kind, binding_digest, artifact_digest, artifact_json,
              status, source_ref, created_at, updated_at
            )
            VALUES (?, 'retrieval_strategy', ?, ?, ?, 'approved', ?, ?, ?)
            """,
            (
                new_id(),
                binding_digest,
                digest,
                _json_text(payload),
                f"synthetic://release-artifact/{binding.candidate_id}",
                now,
                now,
            ),
        )

    def _validate_prepared_evidence(
        self,
        *,
        binding: ReleaseBindingV1,
        validation_report_id: str,
        review_report_id: str,
        evaluation_report_digest: str,
        policy_snapshot_digest: str,
        canary_samples: int,
        proposer_id: str,
        reviewer_id: str,
        reviewer_decision_ref: str,
        validation_report_ref: str,
    ) -> dict[str, Any]:
        self._validate_reviewers(proposer_id=proposer_id, reviewer_id=reviewer_id)
        validation_row = self._connection.execute(
            """
            SELECT proposal_id, fixed_set_result_json, dynamic_set_result_json,
                   latency_cost_json, status
            FROM validation_reports
            WHERE id = ?
            """,
            (validation_report_id,),
        ).fetchone()
        if validation_row is None:
            raise ValueError("prepared release validation report missing")
        if validation_row["status"] != "approved":
            raise ValueError("prepared release validation report must be approved")
        report = _json_obj(validation_row["fixed_set_result_json"])
        self._validate_report_execution(
            report, proposal_id=str(validation_row["proposal_id"]), binding=binding,
        )
        dynamic = _json_obj(validation_row["dynamic_set_result_json"])
        policy = _json_obj(validation_row["latency_cost_json"])
        _validate_canonical_promotion_report(report, binding=binding)
        _validate_dynamic_report(dynamic)
        _validate_policy_snapshot(policy, canary_samples=canary_samples)
        if report.get("report_ref") != validation_report_ref:
            raise ValueError("prepared release validation report ref mismatch")
        if report.get("report_digest") != evaluation_report_digest:
            raise ValueError("prepared release evaluation digest mismatch")
        if policy.get("snapshot_digest") != policy_snapshot_digest:
            raise ValueError("prepared release policy digest mismatch")
        if policy.get("evaluation_report_digest") != report.get("report_digest"):
            raise ValueError("prepared release policy report digest mismatch")

        proposal_row = self._connection.execute(
            """
            SELECT proposer_id, target_component
            FROM evolution_proposals
            WHERE id = ?
            """,
            (str(validation_row["proposal_id"]),),
        ).fetchone()
        if proposal_row is None:
            raise ValueError("prepared release proposal missing")
        if proposal_row["target_component"] != binding.target_component:
            raise ValueError("prepared release target component mismatch")
        if proposal_row["proposer_id"] != proposer_id:
            raise ValueError("prepared release proposer mismatch")

        review_row = self._connection.execute(
            """
            SELECT proposal_id, reviewer_id, decision, rationale, evidence_refs_json
            FROM review_reports
            WHERE id = ?
            """,
            (review_report_id,),
        ).fetchone()
        if review_row is None:
            raise ValueError("prepared release review report missing")
        if review_row["proposal_id"] != validation_row["proposal_id"]:
            raise ValueError("prepared release review proposal mismatch")
        if review_row["reviewer_id"] != reviewer_id:
            raise ValueError("prepared release reviewer mismatch")
        if review_row["decision"] != "approve":
            raise ValueError("prepared release review must be approved")
        if review_row["rationale"] != reviewer_decision_ref:
            raise ValueError("prepared release reviewer decision ref mismatch")
        review_evidence = _json_obj(review_row["evidence_refs_json"])
        if review_evidence.get("source_evidence_refs") != list(binding.source_evidence_refs):
            raise ValueError("prepared release evidence refs mismatch")
        review_artifact = _json_obj(review_evidence.get("review_artifact"))
        review_digest = review_evidence.get("review_artifact_digest")
        if (
            not review_artifact
            or review_digest != f"sha256:{sha256_json(review_artifact)}"
            or review_artifact.get("binding_digest") != binding.canonical_digest()
            or review_artifact.get("artifact_digest") != binding.approved_artifact_digest
            or review_artifact.get("decision") != "approve"
        ):
            raise ValueError("prepared release review artifact mismatch")

        release_gate = {
            "evaluation_report_digest": evaluation_report_digest,
            "fixed_set_snapshot_digest": str(report["fixed_set_snapshot_digest"]),
            "policy_snapshot_digest": policy_snapshot_digest,
            "promotion_eligible": True,
        }
        return {
            "proposal_id": str(validation_row["proposal_id"]),
            "proposer_id": proposer_id,
            "reviewer_id": reviewer_id,
            "release_gate": release_gate,
            "source_evaluation_ids": tuple(_case_ids_from_report(report)),
            "source_trajectory_ids": tuple(policy["source_trajectory_ids"]),
        }

    def _preflight_canary_sample_count(self, release_id: str) -> None:
        """Reject undersampling without requiring prior successful self-evaluation.

        This read-only check cannot authorize promotion. The full integrity and
        fresh observation checks below remain mandatory, including inside the
        promotion transaction. Hydration/serving integrity is deliberately not
        required for this negative command precondition.
        """
        row = self._connection.execute(
            "SELECT * FROM strategy_releases WHERE id = ?", (release_id,),
        ).fetchone()
        if row is None or row["state"] != ReleaseState.CANARY.value:
            return
        spec = self._prepared_spec(release_id)
        binding = spec["binding"]
        head = self._connection.execute(
            "SELECT * FROM strategy_release_heads WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if (
            head is None
            or head["release_state"] != ReleaseState.CANARY.value
            or head["target_component"] != binding.target_component
            or head["binding_digest"] != binding.canonical_digest()
            or head["approved_artifact_digest"] != binding.approved_artifact_digest
            or not self._load_binding(release_id).matches(binding)
            or row["target_component"] != binding.target_component
            or row["rollback_target_release_id"] != binding.rollback_target_id
            or _json_obj(row["canary_scope_json"])
            != spec["canary_assignment"].canonical_payload()
        ):
            raise ValueError("canary preflight binding or assignment mismatch")
        self._validate_binding(binding)
        self._validate_canary_assignment(spec["canary_assignment"])
        rows = self._matching_canary_observation_rows(
            release_id=release_id, binding=binding,
            canary_assignment=spec["canary_assignment"],
        )
        self._require_canary_sample_count(rows, min_samples=int(spec["canary_samples"]))

    def _matching_canary_observation_rows(
        self,
        *,
        release_id: str,
        binding: ReleaseBindingV1,
        canary_assignment: CanaryAssignment,
    ) -> list[sqlite3.Row]:
        return self._connection.execute(
            """
            SELECT tt.id AS trajectory_id, tt.task_family, tt.agent_version,
                   tt.knowledge_version, tt.environment_version, tt.status,
                   tt.evidence_refs_json, te.result_json, te.process_json,
                   te.quality_json, te.failure_tags_json, te.confidence,
                   te.learning_eligible
            FROM task_trajectories tt
            JOIN task_evaluations te ON te.trajectory_id = tt.id
            WHERE tt.task_family = ?
              AND tt.agent_version = ?
              AND json_extract(tt.evidence_refs_json, '$.canary_observation.release_id') = ?
              AND json_extract(tt.evidence_refs_json, '$.canary_observation.binding_digest') = ?
              AND json_extract(tt.evidence_refs_json, '$.canary_observation.cohort') = ?
              AND json_extract(tt.evidence_refs_json, '$.canary_observation.target_component') = ?
              AND json_extract(te.process_json, '$.release_id') = ?
              AND json_extract(te.process_json, '$.release_state') = 'canary'
              AND json_extract(te.process_json, '$.binding_digest') = ?
              AND te.learning_eligible = 1
            """,
            (
                binding.target_component,
                f"release:{release_id}",
                release_id,
                binding.canonical_digest(),
                str(canary_assignment.scope.get("cohort", "")),
                binding.target_component,
                release_id,
                binding.canonical_digest(),
            ),
        ).fetchall()

    @staticmethod
    def _require_canary_sample_count(
        rows: Sequence[sqlite3.Row], *, min_samples: int,
    ) -> None:
        if min_samples < _CANARY_MIN_SAMPLES:
            raise ValueError("canary sample threshold is below the protected minimum")
        if len({row["trajectory_id"] for row in rows}) < min_samples:
            raise ValueError("canary requires real append-only observations before stable")

    def _validate_canary_observations(
        self,
        *,
        release_id: str,
        binding: ReleaseBindingV1,
        canary_assignment: CanaryAssignment,
        min_samples: int,
    ) -> None:
        rows = self._matching_canary_observation_rows(
            release_id=release_id, binding=binding, canary_assignment=canary_assignment,
        )
        self._require_canary_sample_count(rows, min_samples=min_samples)
        for row in rows:
            if row["status"] != "active":
                raise ValueError("canary trajectory is not active")
            if _json_list(row["failure_tags_json"]):
                raise ValueError("canary observation failures block stable promotion")
            evidence_refs = _json_obj(row["evidence_refs_json"])
            result = _json_obj(row["result_json"])
            quality = _json_obj(row["quality_json"])
            if not evidence_refs.get("deployment_hmac_digest"):
                raise ValueError("canary observation integrity evidence missing")
            if not evidence_refs.get("event_chain_digest"):
                raise ValueError("canary observation event chain missing")
            event_hashes = tuple(
                str(item)
                for item in _json_list(evidence_refs.get("event_hashes", []))
            )
            if not event_hashes:
                raise ValueError("canary observation event hashes missing")
            expected_chain = f"sha256:{sha256_text('|'.join(event_hashes))}"
            if evidence_refs.get("event_chain_digest") != expected_chain:
                raise ValueError("canary observation event chain digest mismatch")
            if not evidence_refs.get("request_digest"):
                raise ValueError("canary observation request digest missing")
            trajectory_id = str(row["trajectory_id"])
            self._verify_trajectory_integrity(dict(row), trajectory_id=trajectory_id)
            if result.get("stop_reason") != "completed" or quality.get("completed") is not True:
                raise ValueError("canary observation must contain a completed quality result")


class _RawSQLiteMappingResult:
    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows

    def first(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[sqlite3.Row]:
        return self._rows


class _RawSQLiteResult:
    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows

    def first(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        if len(self._rows) != 1:
            raise ValueError("expected exactly one row")
        return self._rows[0][0]

    def mappings(self) -> _RawSQLiteMappingResult:
        return _RawSQLiteMappingResult(self._rows)


class _RawSQLiteSessionAdapter:
    """Small read-only SQLAlchemy Session surface over the controller connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, statement: Any, params: Mapping[str, Any] | None = None) -> _RawSQLiteResult:
        cursor = self._connection.execute(str(statement), params or {})
        return _RawSQLiteResult(list(cursor.fetchall()))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return cast(dict[str, Any], json.loads(value))
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected JSON object, got {type(value)!r}")


def _json_any(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return decoded
    if isinstance(value, list):
        return value
    return []


def _fixed_set_snapshot_from_report(report: EvaluationReport) -> dict[str, Any]:
    grouped: dict[str, Any] = {name: [] for name in _FIXED_EVAL_SETS}
    for case in sorted(report.case_assessments, key=lambda item: (item.set_name, item.case_id)):
        grouped[case.set_name].append({
            "case_id": case.case_id,
            "evidence_digest": case.evidence_digest,
            "failure_tags": list(case.failure_tags),
            "passed": case.passed,
            "process_score": case.process_score,
            "quality_score": case.quality_score,
            "result_score": case.result_score,
            "set_name": case.set_name,
        })
    return grouped


def _case_ids_from_report(report: dict[str, Any]) -> tuple[str, ...]:
    assessments = _json_list(report.get("case_assessments", []))
    if assessments:
        case_ids = tuple(str(_json_mapping(case)["case_id"]) for case in assessments)
        assert_required_fixed_case_coverage(case_ids)
        return case_ids
    raise ValueError("promotion case assessments are required")




def _canonical_promotion_report(
    *,
    binding: ReleaseBindingV1,
    validation_report_ref: str,
    evaluation_report: EvaluationReport,
    execution_run_id: str,
    execution_record_digest: str,
) -> dict[str, Any]:
    canonical_report = evaluation_report.canonical_payload()
    fixed_sets = _fixed_set_snapshot_from_report(evaluation_report)
    fixed_set_snapshot_digest = _digest_mapping(fixed_sets)
    report = {
        "execution_run_id": execution_run_id,
        "execution_record_digest": execution_record_digest,
        "binding_digest": binding.canonical_digest(),
        "case_assessments": canonical_report["case_assessments"],
        "candidate_only": canonical_report["candidate_only"],
        "evaluator_report_digest": evaluation_report.canonical_digest(),
        "failure_count": canonical_report["failure_count"],
        "fixed_set_coverage": canonical_report["fixed_set_coverage"],
        "fixed_set_snapshot_digest": fixed_set_snapshot_digest,
        "fixed_sets": fixed_sets,
        "promotion_eligible": canonical_report["promotion_eligible"],
        "release_id": evaluation_report.release_id,
        "report_ref": validation_report_ref,
        "schema_version": _PROMOTION_EVAL_SCHEMA,
        "scores": canonical_report["scores"],
    }
    return {"report_digest": _digest_mapping(report), **report}


def _canonical_policy_snapshot(
    *,
    validation_report_ref: str,
    evaluation_report: EvaluationReport,
    promotion_report_digest: str,
    canary_samples: int,
    source_trajectory_ids: tuple[str, ...],
) -> dict[str, Any]:
    fixed_sets = _fixed_set_snapshot_from_report(evaluation_report)
    safety_cases = _json_list(fixed_sets.get("safety", []))
    safety_passed = len(safety_cases) == 4 and all(
        _json_mapping(case).get("passed") is True for case in safety_cases
    )
    budget_passed = canary_samples >= _CANARY_MIN_SAMPLES
    snapshot = {
        "budget_passed": budget_passed,
        "evaluation_report_digest": promotion_report_digest,
        "evaluator_report_digest": evaluation_report.canonical_digest(),
        "fixed_set_snapshot_digest": _digest_mapping(fixed_sets),
        "max_external_calls": 0,
        "max_model_calls": 0,
        "max_output_tokens": 0,
        "max_wall_clock_ms": 0,
        "min_canary_samples": _CANARY_MIN_SAMPLES,
        "report_ref": validation_report_ref,
        "safety_passed": safety_passed,
        "schema_version": _POLICY_SNAPSHOT_SCHEMA,
        "source_evaluation_ids": [case.case_id for case in evaluation_report.case_assessments],
        "source_trajectory_ids": list(source_trajectory_ids),
    }
    return {"snapshot_digest": _digest_mapping(snapshot), **snapshot}


def _validated_artifact_payload_digest(
    binding: ReleaseBindingV1, artifact_payload: dict[str, Any]
) -> str:
    validate_strategy_artifact(artifact_payload)
    digest = artifact_digest(artifact_payload)
    if binding.approved_artifact_digest != digest:
        raise ValueError("approved artifact digest must match canonical artifact content")
    return digest


def _digest_mapping(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{sha256_text(payload)}"


def _validate_canonical_promotion_report(
    report: dict[str, Any],
    *,
    binding: ReleaseBindingV1,
) -> None:
    if report.get("schema_version") != _PROMOTION_EVAL_SCHEMA:
        raise ValueError("canonical promotion evaluation report missing")
    report_digest = str(report.get("report_digest", ""))
    digest_payload = dict(report)
    digest_payload.pop("report_digest", None)
    if report_digest != _digest_mapping(digest_payload):
        raise ValueError("canonical promotion evaluation report digest mismatch")
    if report.get("binding_digest") != binding.canonical_digest():
        raise ValueError("promotion evaluation binding digest mismatch")
    case_payload = {
        "candidate_only": report.get("candidate_only"),
        "case_assessments": report.get("case_assessments"),
        "failure_count": report.get("failure_count"),
        "fixed_set_coverage": report.get("fixed_set_coverage"),
        "learning_eligible": report.get("promotion_eligible"),
        "promotion_eligible": report.get("promotion_eligible"),
        "release_id": report.get("release_id"),
        "scores": report.get("scores"),
        "set_name": "promotion",
    }
    if report.get("evaluator_report_digest") != f"sha256:{sha256_text(_json_text(case_payload))}":
        raise ValueError("promotion evaluator report digest mismatch")
    if report.get("candidate_only") is True or report.get("promotion_eligible") is not True:
        raise ValueError("promotion evaluation report is not eligible")
    if report.get("failure_count") != 0:
        raise ValueError("promotion evaluation failures block stable release")
    scores = _json_mapping(report.get("scores", {}))
    for score_name in ("overall", "process", "quality", "result"):
        if float(scores.get(score_name, 0.0)) < 1.0:
            raise ValueError("promotion evaluation requires 100 percent scores")
    coverage = _json_mapping(report.get("fixed_set_coverage", {}))
    fixed_sets = _json_mapping(report.get("fixed_sets", {}))
    if set(fixed_sets) != set(_FIXED_EVAL_SETS) or set(coverage) != set(_FIXED_EVAL_SETS):
        raise ValueError("promotion evaluation must cover the four fixed sets")
    if report.get("fixed_set_snapshot_digest") != _digest_mapping(fixed_sets):
        raise ValueError("promotion fixed-set snapshot digest mismatch")
    snapshot_cases: list[dict[str, Any]] = []
    for fixed_set in _FIXED_EVAL_SETS:
        if coverage.get(fixed_set) is not True:
            raise ValueError("promotion evaluation missing fixed-set coverage")
        cases = fixed_sets[fixed_set]
        if not isinstance(cases, list) or not cases:
            raise ValueError("fixed-set snapshot must preserve every case")
        for value in cases:
            case = _json_mapping(value)
            if case.get("set_name") != fixed_set:
                raise ValueError("case set does not match promotion fixed set")
            if case.get("passed") is not True:
                raise ValueError("fixed-set promotion case must pass evaluation")
            snapshot_cases.append(case)
    assessments = _json_list(report.get("case_assessments", []))
    assessment_ids: list[str] = []
    for item in assessments:
        case = _json_mapping(item)
        case_id = str(case.get("case_id", ""))
        set_name = str(case.get("set_name", ""))
        assert_registered_fixed_case(case_id, set_name)
        if _json_list(case.get("failure_tags", [])):
            raise ValueError("fixed-set promotion case failure tags must be empty")
        for score_name in ("process_score", "quality_score", "result_score"):
            if float(case.get(score_name, 0.0)) < 1.0:
                raise ValueError("fixed-set promotion case requires 100 percent scores")
        assessment_ids.append(case_id)
    assert_required_fixed_case_coverage(assessment_ids)
    assert_required_fixed_case_coverage(str(case.get("case_id", "")) for case in snapshot_cases)
    if snapshot_cases != [
        {**_json_mapping(case), "passed": not _json_list(_json_mapping(case)["failure_tags"])}
        for name in _FIXED_EVAL_SETS
        for case in assessments if _json_mapping(case).get("set_name") == name
    ]:
        raise ValueError("fixed-set snapshot differs from complete case assessments")


def _validate_dynamic_report(dynamic: dict[str, Any]) -> None:
    if dynamic.get("candidate_only") is not True:
        raise ValueError("dynamic evaluation cannot satisfy fixed promotion gate")
    for case in _json_list(dynamic.get("cases", [])):
        if not isinstance(case, dict):
            raise ValueError("dynamic evaluation case must be an object")
        if (
            case.get("set_name") in _FIXED_EVAL_SETS
            and case.get("independent_reviewed") is not True
        ):
            raise ValueError("dynamic evaluation cannot satisfy fixed promotion gate")


def _validate_policy_snapshot(policy: dict[str, Any], *, canary_samples: int) -> None:
    if policy.get("schema_version") != _POLICY_SNAPSHOT_SCHEMA:
        raise ValueError("risk policy snapshot missing")
    snapshot_digest = str(policy.get("snapshot_digest", ""))
    digest_payload = dict(policy)
    digest_payload.pop("snapshot_digest", None)
    if snapshot_digest != _digest_mapping(digest_payload):
        raise ValueError("risk policy snapshot digest mismatch")
    if policy.get("safety_passed") is not True:
        raise ValueError("safety gate must pass")
    if policy.get("budget_passed") is not True:
        raise ValueError("budget gate must pass")
    source_evaluation_ids = _json_list(policy.get("source_evaluation_ids", []))
    source_trajectory_ids = _json_list(policy.get("source_trajectory_ids", []))
    if not source_evaluation_ids or not source_trajectory_ids:
        raise ValueError("risk policy snapshot must bind evaluation and trajectory sources")
    if int(policy.get("min_canary_samples", _CANARY_MIN_SAMPLES)) > canary_samples:
        raise ValueError("canary requires at least five samples")


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise ValueError("expected JSON object")


def _release_gate_from_input(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    gate = value.get("release_gate")
    if isinstance(gate, dict):
        return gate
    return None


def _release_input_digest(
    *,
    binding: ReleaseBindingV1,
    source_evaluation_ids: tuple[str, ...] | list[str],
    source_trajectory_ids: tuple[str, ...] | list[str],
    release_gate: dict[str, Any],
) -> str:
    return _digest_mapping(
        {
            "binding_digest": binding.canonical_digest(),
            "release_gate": release_gate,
            "source_evaluation_ids": list(source_evaluation_ids),
            "source_trajectory_ids": list(source_trajectory_ids),
        }
    )


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()
