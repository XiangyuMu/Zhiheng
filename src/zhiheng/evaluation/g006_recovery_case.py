"""Destructive fixed case restricted to its caller-owned synthetic work directory."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from zhiheng.core.config import Settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine
from zhiheng.evaluation.g006_preparation import (
    prepare_migrated_database,
    prepare_restic_repository,
)
from zhiheng.knowledge import KnowledgeUserAuthority, TextEvidenceInput
from zhiheng.knowledge.service import KnowledgeIngestionService
from zhiheng.memory import MemoryRepository, MemoryValue
from zhiheng.privacy.erase import PrivacyEraseService
from zhiheng.privacy.erase_journal import ExternalEraseJournal


def execute_recovery_case(
    *, project_root: Path, work_dir: Path
) -> tuple[dict[str, Any], dict[str, bool]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    database, objects = work_dir / "source.sqlite", work_dir / "objects"
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{database}",
        knowledge_object_store_path=str(objects),
    )
    prepare_migrated_database(project_root, database)
    engine = create_sqlite_engine(settings)
    factory = create_session_factory(engine)
    facts: dict[str, Any] = {}
    outcomes = {
        key: False
        for key in (
            "delete.pass_rate_100",
            "rollback.pass_rate_100",
            "privacy_erase.pass_rate_100",
            "backup_restore.erased_object_not_revived",
        )
    }
    secret = "protected-synthetic-recovery-journal-secret"
    journal = work_dir / "erase.jsonl"
    try:
        memory = MemoryRepository()
        with factory.begin() as session:
            initial = memory.commit_explicit_memory(
                session,
                MemoryValue("goal", "goal.recovery", {"text": "original"}),
                operation_key="recovery-initial",
            )
            formal_id, version = str(initial.formal_memory_id), str(initial.formal_version_id)
            memory.soft_delete(session, formal_memory_id=formal_id, operation_key="delete")
            deleted = not memory.l0_context(session)
            memory.restore(session, formal_memory_id=formal_id, operation_key="restore")
            restored = memory.l0_context(session) == {"goal.recovery": {"text": "original"}}
            memory.commit_explicit_memory(
                session,
                MemoryValue("goal", "goal.recovery", {"text": "changed"}),
                operation_key="recovery-change",
            )
            memory.rollback(
                session,
                formal_memory_id=formal_id,
                target_version_id=version,
                operation_key="rollback",
            )
            outcomes["delete.pass_rate_100"] = deleted and restored
            outcomes["rollback.pass_rate_100"] = memory.l0_context(session) == {
                "goal.recovery": {"text": "original"}
            }
        item = KnowledgeIngestionService(settings).ingest_user_text(
            factory,
            TextEvidenceInput(
                title="synthetic recovery",
                text="synthetic erasable evidence sentinel",
                primary_domain_id="technology.ai",
            ),
            user_authority=KnowledgeUserAuthority("protected-synthetic-user"),
        )
        paths = [path for path in objects.rglob("*") if path.is_file()]
        engine.dispose()
        binary = os.environ.get("ZHIHENG_RESTIC_BINARY") or shutil.which("restic")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(project_root / "src"),
            "ZHIHENG_ENVIRONMENT": "test",
            "ZHIHENG_SECRET_KEY": secret,
            "RESTIC_REPOSITORY": str(work_dir / "repository"),
            "RESTIC_PASSWORD": "protected-synthetic-backup-password",
            "RESTIC_CACHE_DIR": str(work_dir / "restic-cache"),
            "ZHIHENG_DATABASE_PATH": str(database),
            "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": str(objects),
        }
        snapshot = None
        if binary:
            env["ZHIHENG_RESTIC_BINARY"] = binary
            prepare_restic_repository(
                binary=binary,
                repository=work_dir / "repository",
                project_root=project_root,
                environment=env,
                run=_run,
            )
            snapshot = json.loads(
                _run(
                    [sys.executable, "scripts/backup_restic.py"],
                    project_root,
                    env,
                )
            )["snapshot_id"]
        else:
            facts["backup_failure"] = "restic_not_configured"
        erase = PrivacyEraseService(
            ExternalEraseJournal(journal, secret),
            object_store_root=objects,
        )
        with factory() as session:
            intent = erase.request_erase(
                session,
                target_type="knowledge_object",
                target_id=item.knowledge_object_id,
                requester="user",
                reason="synthetic recovery probe",
            )
            erase.execute_knowledge_erase(
                session,
                request_id=intent.request_id,
                knowledge_object_id=item.knowledge_object_id,
            )
            session.commit()
            intent = erase.request_erase(
                session,
                target_type="formal_memory",
                target_id=formal_id,
                requester="user",
                reason="synthetic recovery probe",
            )
            erase.execute_memory_erase(
                session,
                request_id=intent.request_id,
                target_type="formal_memory",
                target_id=formal_id,
            )
            session.commit()
            memory_hidden = not memory.l0_context(session)
        engine.dispose()
        outcomes["privacy_erase.pass_rate_100"] = (
            len(paths) == 3 and all(not path.exists() for path in paths) and memory_hidden
        )
        facts["erased_artifact_count"] = sum(not path.exists() for path in paths)
        if snapshot:
            target_db, target_objects = work_dir / "restored.sqlite", work_dir / "restored-objects"
            env.update(
                {
                    "ZHIHENG_DATABASE_PATH": str(target_db),
                    "ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH": str(target_objects),
                    "ZHIHENG_ERASE_JOURNAL_PATH": str(journal),
                    "ZHIHENG_RESTIC_SNAPSHOT_ID": snapshot,
                }
            )
            _run([sys.executable, "scripts/restore_restic.py"], project_root, env)
            with closing(sqlite3.connect(target_db)) as connection:
                visible = connection.execute(
                    "SELECT (SELECT count(*) FROM current_formal_knowledge) + "
                    "(SELECT count(*) FROM current_formal_memory)"
                ).fetchone()[0]
            survivors = sum(path.is_file() for path in target_objects.rglob("*"))
            facts.update(restored_visible_objects=visible, restored_object_files=survivors)
            outcomes["backup_restore.erased_object_not_revived"] = visible == 0 and survivors == 0
        return facts, outcomes
    finally:
        engine.dispose()


def _run(args: list[str], root: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        args,
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        # These subprocesses operate only on code-owned synthetic fixtures.
        # Preserve their diagnostics: CalledProcessError's default text omits
        # captured stderr and otherwise makes intermittent restore failures opaque.
        raise RuntimeError(
            f"synthetic recovery subprocess failed ({result.returncode}): {result.stderr[-8000:]}"
        )
    return result.stdout
