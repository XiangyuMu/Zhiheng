from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from zhiheng.core.config import Settings
from zhiheng.knowledge.object_store import (
    LocalKnowledgeObjectStore,
    knowledge_object_store_for_settings,
)


def test_identical_evidence_has_independent_private_files(tmp_path: Path) -> None:
    store = LocalKnowledgeObjectStore(tmp_path)
    first = store.write_text_artifacts("合成原始证据")
    second = store.write_text_artifacts("合成原始证据")
    assert first.sha256 == second.sha256
    assert first.evidence_object_uri != second.evidence_object_uri
    first_path = Path(unquote(urlparse(first.evidence_object_uri).path))
    second_path = Path(unquote(urlparse(second.evidence_object_uri).path))
    assert first_path.stat().st_ino != second_path.stat().st_ino
    assert first_path.stat().st_mode & 0o777 == 0o600
    first_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.verify_text_artifacts(first)
    store.verify_text_artifacts(second)


def test_object_store_rejects_remote_file_authority(tmp_path: Path) -> None:
    store = LocalKnowledgeObjectStore(tmp_path)
    artifacts = store.write_text_artifacts("synthetic")
    remote = replace(
        artifacts,
        evidence_object_uri=artifacts.evidence_object_uri.replace("file:///", "file://remote/"),
    )
    with pytest.raises(ValueError, match="unsupported object store uri"):
        store.verify_text_artifacts(remote)


def test_relative_sqlite_url_keeps_store_relative_to_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(database_url="sqlite:///./data/database.sqlite")
    store = knowledge_object_store_for_settings(settings)
    assert store.root == tmp_path / "data" / "knowledge-object-store"


def test_memory_database_requires_explicit_object_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZHIHENG_KNOWLEDGE_OBJECT_STORE_PATH", raising=False)
    with pytest.raises(ValueError, match="explicit knowledge object store"):
        knowledge_object_store_for_settings(
            Settings(database_url="sqlite:///:memory:", _env_file=None)
        )
