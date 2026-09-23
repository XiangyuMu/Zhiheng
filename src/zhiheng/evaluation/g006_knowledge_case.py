"""Local boundary probe using real ingestion, FTS authorization and answer adapter."""

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from zhiheng.api.retrieval import EvidenceBoundAnswerModel
from zhiheng.core.config import Settings
from zhiheng.core.ids import new_id, sha256_json
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evolution.artifacts import (
    artifact_digest,
    validate_serving_strategy_artifact,
    validate_strategy_artifact,
)
from zhiheng.knowledge import KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.service import KnowledgeIngestionService
from zhiheng.retrieval import (
    CitationBuilder,
    HybridRetriever,
    QueryRoute,
    QueryRouter,
    RetrievalAuthorizer,
)


def execute_knowledge_boundary(
    *,
    project_root: Path,
    work_dir: Path,
    artifact: dict[str, Any],
    scenario: str = "boundary",
) -> tuple[dict[str, Any], dict[str, bool]]:
    if scenario == "migration":
        validate_serving_strategy_artifact(artifact)
    else:
        validate_strategy_artifact(artifact)
    seed = _seed_knowledge_snapshot(
        project_root=project_root,
        work_dir=work_dir,
        scenario=scenario,
    )
    return _run_knowledge_query(
        db_path=seed["db_path"],
        object_store_path=seed["object_store_path"],
        artifact=artifact,
        query=str(seed["query"]),
        contents=tuple(seed["contents"]),
        current_chunk_ids=set(seed["current_chunk_ids"]),
        stale_chunk_id=str(seed["stale_chunk_id"]),
        snapshot_digest=str(seed["snapshot_digest"]),
    )


def execute_knowledge_shadow(
    *,
    project_root: Path,
    work_dir: Path,
    baseline_artifact: dict[str, Any],
    candidate_artifact: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, bool]]:
    validate_serving_strategy_artifact(baseline_artifact)
    validate_strategy_artifact(candidate_artifact)
    seed = _seed_knowledge_snapshot(
        project_root=project_root,
        work_dir=work_dir / "seed",
        scenario="boundary",
    )
    baseline_snapshot = _copy_snapshot(
        source_db=seed["db_path"],
        source_objects=seed["object_store_path"],
        target_dir=work_dir / "baseline",
    )
    candidate_snapshot = _copy_snapshot(
        source_db=seed["db_path"],
        source_objects=seed["object_store_path"],
        target_dir=work_dir / "candidate",
    )
    baseline_facts, baseline_outcomes = _run_knowledge_query(
        db_path=baseline_snapshot["db_path"],
        object_store_path=baseline_snapshot["object_store_path"],
        artifact=baseline_artifact,
        query=str(seed["query"]),
        contents=tuple(seed["contents"]),
        current_chunk_ids=set(seed["current_chunk_ids"]),
        stale_chunk_id=str(seed["stale_chunk_id"]),
        snapshot_digest=str(baseline_snapshot["snapshot_digest"]),
    )
    candidate_facts, candidate_outcomes = _run_knowledge_query(
        db_path=candidate_snapshot["db_path"],
        object_store_path=candidate_snapshot["object_store_path"],
        artifact=candidate_artifact,
        query=str(seed["query"]),
        contents=tuple(seed["contents"]),
        current_chunk_ids=set(seed["current_chunk_ids"]),
        stale_chunk_id=str(seed["stale_chunk_id"]),
        snapshot_digest=str(candidate_snapshot["snapshot_digest"]),
    )
    source_digest_after = _snapshot_digest(seed["db_path"], seed["object_store_path"])
    baseline_quality = sum(bool(value) for value in baseline_outcomes.values())
    candidate_quality = sum(bool(value) for value in candidate_outcomes.values())
    facts = {
        "baseline": baseline_facts,
        "baseline_artifact_digest": artifact_digest(baseline_artifact),
        "baseline_budget": _budget_facts(baseline_facts),
        "baseline_outcomes": baseline_outcomes,
        "candidate": candidate_facts,
        "candidate_artifact_digest": artifact_digest(candidate_artifact),
        "candidate_budget": _budget_facts(candidate_facts),
        "candidate_outcomes": candidate_outcomes,
        "candidate_quality_score": candidate_quality,
        "baseline_quality_score": baseline_quality,
        "copy_method": "sqlite_backup_plus_shared_seed_object_uris",
        "input_digest": seed["input_digest"],
        "object_store_mode": "shared_seed_object_store_no_write_requests",
        "same_database_snapshot_digest": (
            baseline_snapshot["snapshot_digest"] == candidate_snapshot["snapshot_digest"]
        ),
        "source_snapshot_digest": seed["snapshot_digest"],
        "source_snapshot_digest_after": source_digest_after,
        "source_snapshot_unchanged": source_digest_after == seed["snapshot_digest"],
        "snapshot_transforms": [
            baseline_snapshot["transform"],
            candidate_snapshot["transform"],
        ],
        "target_component": "retrieval.answer_strategy",
    }
    outcomes = {
        "shadow.same_snapshot_input": (
            facts["same_database_snapshot_digest"]
            and baseline_facts["input_digest"] == candidate_facts["input_digest"]
        ),
        "shadow.baseline_unpolluted": bool(facts["source_snapshot_unchanged"]),
        "shadow.candidate_quality_regression_detected": candidate_quality < baseline_quality,
        "shadow.local_fts_profile_only": (
            baseline_facts.get("retrieval_profile") == "real_fts_vector_unavailable"
            and (
                candidate_facts.get("retrieval_profile") == "real_fts_vector_unavailable"
                or candidate_facts.get("unsupported_route") is not None
            )
        ),
    }
    facts["observation_digest"] = sha256_json({"facts": facts, "outcomes": outcomes})
    return facts, outcomes


def _seed_knowledge_snapshot(
    *,
    project_root: Path,
    work_dir: Path,
    scenario: str,
) -> dict[str, Any]:
    scenarios = {
        "boundary": (
            "缓存策略",
            (
                "缓存策略：系统必须启用缓存。",
                "缓存策略：系统必须禁用缓存。",
                "缓存策略：旧版本哨兵，不得作为当前证据。",
            ),
        ),
        "migration": (
            "资料共享",
            (
                "资料共享：研究团队必须共享资料。",
                "资料共享：研究团队不得共享资料。",
                "资料共享：过期版本哨兵，不得作为当前证据。",
            ),
        ),
    }
    query, contents = scenarios[scenario]
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "knowledge.sqlite"
    object_store_path = work_dir / "objects"
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        knowledge_object_store_path=str(object_store_path),
    )
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    engine = create_sqlite_engine(settings)
    factory = create_session_factory(engine)
    try:
        ingestion = KnowledgeIngestionService(settings)
        items = [
            ingestion.ingest_user_text(
                factory,
                TextEvidenceInput(
                    title=f"synthetic policy {index}",
                    text=content,
                    primary_domain_id="technology.ai",
                ),
                user_authority=KnowledgeUserAuthority("protected-synthetic-user"),
            )
            for index, content in enumerate(contents)
        ]
        stale = items[-1]
        with factory.begin() as session:
            replacement = new_id()
            session.execute(
                text(
                    "INSERT INTO knowledge_versions "
                    "(id, knowledge_object_id, version_no, content_version_id, markdown_uri, "
                    "summary, source_quality) "
                    "SELECT :replacement, knowledge_object_id, 2, content_version_id, "
                    "markdown_uri, summary, source_quality FROM knowledge_versions WHERE id = :old"
                ),
                {"replacement": replacement, "old": stale.knowledge_version_id},
            )
            session.execute(
                text(
                    "UPDATE knowledge_objects SET current_version_id = :replacement WHERE id = :id"
                ),
                {"replacement": replacement, "id": stale.knowledge_object_id},
            )
    finally:
        engine.dispose()
    seed = {
        "contents": contents,
        "current_chunk_ids": [item.chunk_id for item in items[:2]],
        "db_path": db_path,
        "input_digest": sha256_json({"query": query, "contents": contents}),
        "object_store_path": object_store_path,
        "query": query,
        "stale_chunk_id": stale.chunk_id,
    }
    seed["snapshot_digest"] = _snapshot_digest(db_path, object_store_path)
    return seed


def _run_knowledge_query(
    *,
    db_path: Path,
    object_store_path: Path,
    artifact: dict[str, Any],
    query: str,
    contents: tuple[str, ...],
    current_chunk_ids: set[str],
    stale_chunk_id: str,
    snapshot_digest: str,
) -> tuple[dict[str, Any], dict[str, bool]]:
    override = artifact["routing"]["route_override"]
    route = QueryRouter().route(
        query,
        route_override=QueryRoute(override) if override is not None else None,
    )
    if route.route is not QueryRoute.HYBRID:
        facts = {
            "artifact_digest": artifact_digest(artifact),
            "estimated_output_tokens": 0,
            "input_digest": sha256_json({"query": query, "contents": contents}),
            "query_wall_clock_ms": 0,
            "retrieval_profile": "real_fts_vector_unavailable",
            "snapshot_digest": snapshot_digest,
            "unsupported_route": route.route.value,
        }
        outcomes = {
            name: False
            for name in (
                "rag.recall_at_10",
                "rag.citation_coverage",
                "rag.conflict_detected",
                "rag.stale_evidence_not_authoritative",
            )
        }
        facts["observation_digest"] = sha256_json({"facts": facts, "outcomes": outcomes})
        return facts, outcomes
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path}",
        knowledge_object_store_path=str(object_store_path),
    )
    engine = create_sqlite_engine(settings)
    factory = create_session_factory(engine)
    try:
        started = time.monotonic()
        with factory.begin() as session:
            result = HybridRetriever().search_offline(
                session,
                query,
                limit=10,
                overfetch_factor=artifact["retrieval"]["overfetch_factor"],
                rrf_k=artifact["retrieval"]["rrf_k"],
            )
            manifest = result.manifest
        citations = tuple(
            CitationBuilder().build(
                manifest,
                chunk_id=chunk.chunk_id,
                start_offset=chunk.span_start,
                end_offset=chunk.span_end,
            )
            for chunk in manifest.chunks
        )
        generated = EvidenceBoundAnswerModel().generate_answer(
            query=query,
            manifest=manifest,
            citations=citations,
        )
        with factory.begin() as session:
            authorized = RetrievalAuthorizer().validate_manifest(session, manifest)
        expected = current_chunk_ids
        recalled = {chunk.chunk_id for chunk in manifest.chunks}
        cited = {citation.citation_id for citation in citations}
        coverage = bool(generated.claims) and all(
            claim.citation_ids and set(claim.citation_ids) <= cited for claim in generated.claims
        )
        facts = {
            "artifact_digest": artifact_digest(artifact),
            "input_digest": sha256_json({"query": query, "contents": contents}),
            "query_wall_clock_ms": (time.monotonic() - started) * 1000,
            "estimated_output_tokens": max(
                1,
                len(
                    generated.answer
                    + "".join(generated.conflicts)
                    + "".join(generated.insufficiencies)
                    + "".join(claim.text for claim in generated.claims)
                )
                // 4,
            ),
            "retrieved_chunks": len(manifest.chunks),
            "expected_current_chunks": len(expected),
            "recall_at_10": len(expected & recalled) / len(expected),
            "citation_count": len(citations),
            "claim_count": len(generated.claims),
            "conflict_count": len(generated.conflicts),
            "snapshot_digest": snapshot_digest,
            "stale_chunk_recalled": stale_chunk_id in recalled,
            "final_authorized": authorized,
            "retrieval_profile": "real_fts_vector_unavailable",
        }
        outcomes = {
            "rag.recall_at_10": expected <= recalled,
            "rag.citation_coverage": bool(coverage) and authorized,
            "rag.conflict_detected": bool(generated.conflicts),
            "rag.stale_evidence_not_authoritative": stale_chunk_id not in recalled and authorized,
        }
        facts["observation_digest"] = sha256_json({"facts": facts, "outcomes": outcomes})
        return facts, outcomes
    finally:
        engine.dispose()


def _copy_snapshot(
    *,
    source_db: Path,
    source_objects: Path,
    target_dir: Path,
) -> dict[str, Any]:
    if target_dir.exists():
        raise FileExistsError(f"shadow snapshot target already exists: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_db = target_dir / "knowledge.sqlite"
    with (
        closing(sqlite3.connect(source_db)) as source,
        closing(sqlite3.connect(target_db)) as target,
    ):
        source.backup(target)
    return {
        "db_path": target_db,
        "object_store_path": source_objects,
        "snapshot_digest": _snapshot_digest(target_db, source_objects),
        "transform": {
            "method": "sqlite_backup_plus_shared_seed_object_uris",
            "object_uri_strategy": "shared_seed_file_uris_read_only",
            "source_db_digest": _file_digest(source_db),
            "target_db_digest": _file_digest(target_db),
        },
    }


def _snapshot_digest(db_path: Path, object_store_path: Path) -> str:
    payload = hashlib.sha256()
    payload.update(_file_digest(db_path).encode("utf-8"))
    for path in sorted(item for item in object_store_path.rglob("*") if item.is_file()):
        payload.update(str(path.relative_to(object_store_path)).encode("utf-8"))
        payload.update(_file_digest(path).encode("utf-8"))
    return f"sha256:{payload.hexdigest()}"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _budget_facts(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "estimated_output_tokens": facts["estimated_output_tokens"],
        "query_wall_clock_ms": facts["query_wall_clock_ms"],
        "retrieved_chunks": facts.get("retrieved_chunks", 0),
    }
