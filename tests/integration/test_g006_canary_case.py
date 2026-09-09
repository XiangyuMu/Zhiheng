from __future__ import annotations

from pathlib import Path
from typing import Any

from zhiheng.evaluation import g006_canary_case
from zhiheng.evaluation.g006_canary_case import execute_canary_case
from zhiheng.evolution.artifacts import default_release_artifact
from zhiheng.evolution.releases import ReleaseController
from zhiheng.evolution.trajectory_repository import TrajectoryRepository


def test_g006_canary_case_blocks_undersampled_canary_before_stable(
    tmp_path: Path,
) -> None:
    facts, outcomes = execute_canary_case(
        project_root=Path.cwd(),
        work_dir=tmp_path,
        candidate_id="candidate-canary-preflight",
        target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )

    assert outcomes == {
        "release.insufficient_canary_samples_blocks_stable": True,
        "release.binding_complete": True,
        "release.no_stable_promotion": True,
    }
    assert facts["sample_counts"] == [0, 1, 2, 3, 4]
    assert all(
        attempt["error_message"]
        == "canary requires real append-only observations before stable"
        for attempt in facts["attempts"]
    )
    assert all(
        attempt["stable_head_before"] == attempt["stable_head_after"]
        for attempt in facts["attempts"]
    )
    assert [attempt["observed_sample_count"] for attempt in facts["attempts"]] == [
        0,
        1,
        2,
        3,
        4,
    ]
    assert all(attempt["probe_remained_canary"] for attempt in facts["attempts"])
    assert all(attempt["protected_execution_run_count"] == 0 for attempt in facts["attempts"])


def test_g006_canary_case_fails_if_preflight_is_removed(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def _skip_preflight(self: ReleaseController, *args: Any, **kwargs: Any) -> None:
        del self, args, kwargs

    monkeypatch.setattr(
        ReleaseController,
        "_preflight_canary_sample_count",
        _skip_preflight,
    )

    facts, outcomes = execute_canary_case(
        project_root=Path.cwd(),
        work_dir=tmp_path,
        candidate_id="candidate-canary-no-preflight",
        target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )

    assert outcomes["release.insufficient_canary_samples_blocks_stable"] is False
    assert outcomes["release.no_stable_promotion"] is True
    assert all(
        attempt["error_message"]
        != "canary requires real append-only observations before stable"
        for attempt in facts["attempts"]
    )


def test_g006_canary_case_fails_if_observations_are_not_persisted(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    def _skip_ingest(self: TrajectoryRepository, *args: Any, **kwargs: Any) -> None:
        del self, args, kwargs

    monkeypatch.setattr(TrajectoryRepository, "ingest", _skip_ingest)

    facts, outcomes = execute_canary_case(
        project_root=Path.cwd(),
        work_dir=tmp_path,
        candidate_id="candidate-canary-no-ingest",
        target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )

    assert outcomes["release.insufficient_canary_samples_blocks_stable"] is False
    assert outcomes["release.binding_complete"] is True
    assert [attempt["observed_sample_count"] for attempt in facts["attempts"]] == [
        0,
        0,
        0,
        0,
        0,
    ]


def test_g006_canary_case_does_not_authorize_five_observation_probe(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(g006_canary_case, "_CANARY_SAMPLE_COUNTS", (5,))

    facts, outcomes = execute_canary_case(
        project_root=Path.cwd(),
        work_dir=tmp_path,
        candidate_id="candidate-canary-five-samples",
        target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )

    assert outcomes["release.insufficient_canary_samples_blocks_stable"] is False
    assert outcomes["release.binding_complete"] is True
    assert outcomes["release.no_stable_promotion"] is True
    assert facts["attempts"][0]["observed_sample_count"] == 5
    assert facts["attempts"][0]["error_message"] == "approved release artifact missing"
    assert facts["attempts"][0]["protected_execution_run_count"] == 0


def test_promotion_rejects_caller_owned_deferred_transaction(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    original = g006_canary_case._insert_negative_canary_probe

    def with_open_transaction(**kwargs: Any) -> str:
        release_id = original(**kwargs)
        kwargs["connection"].execute("BEGIN DEFERRED")
        return release_id

    monkeypatch.setattr(g006_canary_case, "_CANARY_SAMPLE_COUNTS", (0,))
    monkeypatch.setattr(g006_canary_case, "_insert_negative_canary_probe", with_open_transaction)
    facts, outcomes = execute_canary_case(
        project_root=Path.cwd(), work_dir=tmp_path, candidate_id="deferred-transaction",
        target_component="retrieval.answer_strategy",
        artifact_payload=default_release_artifact(),
    )
    assert facts["attempts"][0]["error_message"] == (
        "promotion requires a transaction-free connection"
    )
    assert outcomes["release.no_stable_promotion"] is True
