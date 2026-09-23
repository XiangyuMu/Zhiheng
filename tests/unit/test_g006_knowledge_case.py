from pathlib import Path

import pytest

from zhiheng.evaluation.g006_knowledge_case import execute_knowledge_boundary
from zhiheng.evolution.artifacts import default_release_artifact


@pytest.mark.parametrize("scenario", ["boundary", "migration"])
def test_real_boundary_probe_reports_explicit_conflict_with_valid_citations(
    tmp_path: Path, scenario: str
) -> None:
    facts, outcomes = execute_knowledge_boundary(
        project_root=Path.cwd(),
        work_dir=tmp_path,
        artifact=default_release_artifact(),
        scenario=scenario,
    )
    assert facts["retrieval_profile"] == "real_fts_vector_unavailable"
    assert facts["recall_at_10"] == 1.0
    assert facts["citation_count"] == 2
    assert facts["stale_chunk_recalled"] is False
    assert facts["final_authorized"] is True
    assert outcomes == {
        "rag.recall_at_10": True,
        "rag.citation_coverage": True,
        "rag.conflict_detected": True,
        "rag.stale_evidence_not_authoritative": True,
    }
