from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from tests.integration.test_g005_api_impl import _client
from zhiheng.evolution.orphan_gc import OrphanArtifactGC


def test_orphan_gc_requires_signed_plan_and_protects_referenced_drafts(tmp_path: Path) -> None:
    client, factory = _client(tmp_path)
    del client
    old = datetime.now(UTC) - timedelta(days=10)
    with factory() as session:
        for artifact_id, source_ref in (("orphan", None), ("referenced", "orphan")):
            session.execute(text(
                """INSERT INTO evolution_artifacts
                   (id, artifact_kind, binding_digest, artifact_digest,
                    artifact_json, status, source_ref, created_at, updated_at)
                   VALUES (:id, 'strategy_proposal_draft', 'x', 'y',
                           :payload, 'draft', :source_ref, :created, :created)"""),
                {"id": artifact_id, "source_ref": source_ref, "created": old,
                 "payload": json.dumps({"target_component": "retrieval.answer_strategy"})},
            )
        session.commit()
        gc = OrphanArtifactGC("synthetic-gc-key", retention_seconds=1)
        plan = gc.prepare(session, now=int(datetime.now(UTC).timestamp()))
        assert plan.artifact_ids == ("referenced",)
        assert gc.reap(session, plan, now=int(datetime.now(UTC).timestamp())) == 1
        assert session.execute(
            text("SELECT count(*) FROM evolution_artifacts WHERE id='referenced'")
        ).scalar_one() == 0
        assert session.execute(
            text("SELECT count(*) FROM evolution_artifacts WHERE id='orphan'")
        ).scalar_one() == 1
