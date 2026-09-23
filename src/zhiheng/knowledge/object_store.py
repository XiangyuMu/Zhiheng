from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from zhiheng.core.config import Settings

__all__ = [
    "StoredBinaryArtifact",
    "StoredTextArtifacts",
    "LocalKnowledgeObjectStore",
    "knowledge_object_store_for_settings",
]


@dataclass(frozen=True)
class StoredBinaryArtifact:
    uri: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class StoredTextArtifacts:
    evidence_object_uri: str
    text_artifact_uri: str
    markdown_uri: str
    sha256: str
    byte_size: int


class LocalKnowledgeObjectStore:
    """Immutable local object store for knowledge evidence and derived text artifacts."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def write_text_artifacts(self, text_value: str) -> StoredTextArtifacts:
        body = text_value.encode("utf-8")
        body_hash = hashlib.sha256(body).hexdigest()
        group_id = uuid4().hex

        evidence_path = self.root / "evidence" / f"{group_id}.bin"
        content_path = self.root / "artifacts" / "content" / f"{group_id}.md"
        knowledge_path = self.root / "artifacts" / "knowledge" / f"{group_id}.md"

        self._write_immutable(evidence_path, body)
        self._write_immutable(content_path, body)
        self._write_immutable(knowledge_path, body)

        artifacts = StoredTextArtifacts(
            evidence_object_uri=evidence_path.as_uri(),
            text_artifact_uri=content_path.as_uri(),
            markdown_uri=knowledge_path.as_uri(),
            sha256=body_hash,
            byte_size=len(body),
        )
        self.verify_text_artifacts(artifacts)
        return artifacts

    def write_binary_artifact(
        self, body: bytes, *, namespace: str = "source"
    ) -> StoredBinaryArtifact:
        body_hash = hashlib.sha256(body).hexdigest()
        path = self.root / namespace / f"{body_hash}.bin"
        if path.exists():
            existing = path.read_bytes()
            if existing != body:
                raise ValueError(f"immutable object hash collision: {path}")
        else:
            self._write_immutable(path, body)
        artifact = StoredBinaryArtifact(
            uri=path.as_uri(),
            sha256=body_hash,
            byte_size=len(body),
        )
        self.verify_binary_artifact(artifact)
        return artifact

    def verify_binary_artifact(self, artifact: StoredBinaryArtifact) -> None:
        path = self._path_from_uri(artifact.uri)
        actual = path.read_bytes()
        if len(actual) != artifact.byte_size:
            raise ValueError(f"stored binary byte size mismatch: {artifact.uri}")
        if hashlib.sha256(actual).hexdigest() != artifact.sha256:
            raise ValueError(f"stored binary hash mismatch: {artifact.uri}")

    def read_bytes(self, uri: str) -> bytes:
        """Read an immutable object after enforcing the configured store root."""

        return self._path_from_uri(uri).read_bytes()

    def verify_text_artifacts(self, artifacts: StoredTextArtifacts) -> None:
        for uri in (
            artifacts.evidence_object_uri,
            artifacts.text_artifact_uri,
            artifacts.markdown_uri,
        ):
            path = self._path_from_uri(uri)
            actual = path.read_bytes()
            actual_hash = hashlib.sha256(actual).hexdigest()
            if actual_hash != artifacts.sha256:
                raise ValueError(f"stored artifact hash mismatch: {uri}")
            if uri == artifacts.evidence_object_uri and len(actual) != artifacts.byte_size:
                raise ValueError(f"stored evidence byte size mismatch: {uri}")

    def list_orphan_candidates(self, session: Session) -> list[str]:
        referenced = {
            str(uri)
            for uri in session.execute(
                text(
                    """
                    SELECT object_uri AS uri FROM evidence_objects
                    UNION
                    SELECT text_artifact_uri AS uri FROM content_versions
                    UNION
                    SELECT markdown_uri AS uri FROM knowledge_versions
                    WHERE markdown_uri IS NOT NULL
                    """
                )
            ).scalars()
        }
        return sorted(
            path.as_uri()
            for path in self.root.rglob("*")
            if path.is_file() and ".tmp-" not in path.name and path.as_uri() not in referenced
        )

    def _path_from_uri(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(f"unsupported object store uri: {uri}")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"object store uri is outside configured root: {uri}")
        return path

    @staticmethod
    def _write_immutable(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
        try:
            descriptor = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(tmp_path, path)
            parent_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except FileExistsError as exc:
            raise FileExistsError(f"immutable object already exists: {path}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)


def knowledge_object_store_for_settings(settings: Settings) -> LocalKnowledgeObjectStore:
    root = settings.knowledge_object_store_path
    if root is not None:
        return LocalKnowledgeObjectStore(Path(root))
    database_path = _sqlite_database_path(settings)
    return LocalKnowledgeObjectStore(database_path.parent / "knowledge-object-store")


def _sqlite_database_path(settings: Settings) -> Path:
    parsed = make_url(settings.database_url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("knowledge object store derivation requires sqlite database_url")
    if not parsed.database or parsed.database in {":memory:", "/:memory:"}:
        raise ValueError("in-memory databases require an explicit knowledge object store path")
    return Path(parsed.database).expanduser().resolve()
