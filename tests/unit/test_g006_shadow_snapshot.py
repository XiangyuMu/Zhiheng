from pathlib import Path

from zhiheng.evaluation.g006_knowledge_case import execute_knowledge_shadow
from zhiheng.evolution.artifacts import default_release_artifact


def test_shadow_uses_same_snapshot_and_detects_candidate_quality_regression(
    tmp_path: Path,
) -> None:
    baseline_artifact = default_release_artifact()
    candidate_artifact = default_release_artifact()
    candidate_artifact["routing"]["route_override"] = "structured"

    facts, outcomes = execute_knowledge_shadow(
        project_root=Path.cwd(),
        work_dir=tmp_path,
        baseline_artifact=baseline_artifact,
        candidate_artifact=candidate_artifact,
    )

    assert outcomes == {
        "shadow.same_snapshot_input": True,
        "shadow.baseline_unpolluted": True,
        "shadow.candidate_quality_regression_detected": True,
        "shadow.local_fts_profile_only": True,
    }
    assert facts["baseline_outcomes"] == {
        "rag.recall_at_10": True,
        "rag.citation_coverage": True,
        "rag.conflict_detected": True,
        "rag.stale_evidence_not_authoritative": True,
    }
    assert facts["candidate_outcomes"] == {
        "rag.recall_at_10": False,
        "rag.citation_coverage": False,
        "rag.conflict_detected": False,
        "rag.stale_evidence_not_authoritative": False,
    }
    assert facts["baseline"]["snapshot_digest"] == facts["candidate"]["snapshot_digest"]
    assert facts["baseline"]["input_digest"] == facts["candidate"]["input_digest"]
    assert facts["baseline"]["artifact_digest"] == facts["baseline_artifact_digest"]
    assert facts["candidate"]["artifact_digest"] == facts["candidate_artifact_digest"]
    assert len(facts["baseline"]["observation_digest"]) == 64
    assert len(facts["candidate"]["observation_digest"]) == 64
    assert len(facts["observation_digest"]) == 64
    assert facts["object_store_mode"] == "shared_seed_object_store_no_write_requests"
    assert facts["source_snapshot_digest"] == facts["source_snapshot_digest_after"]
    assert facts["candidate"]["unsupported_route"] == "structured"
