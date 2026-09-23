from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = "privacy_erase_journal.v1"
PENDING_SCHEMA_VERSION = "privacy_erase_journal_pending.v1"
GENESIS_DIGEST = "0" * 64
JOURNAL_ENV_VAR = "ZHIHENG_ERASE_JOURNAL_PATH"
KEY_ENV_VAR = "ZHIHENG_SECRET_KEY"


class EraseJournalError(ValueError):
    pass


@dataclass(frozen=True)
class EraseJournalRecord:
    seq: int
    request_id: str
    target_type: str
    target_id: str
    prev_digest: str
    digest: str


class ExternalEraseJournal:
    def __init__(self, path: Path, key: str) -> None:
        if not key:
            raise EraseJournalError(f"{KEY_ENV_VAR} must be set")
        self._path = path
        self._key = key.encode("utf-8")

    @classmethod
    def from_env(cls) -> ExternalEraseJournal:
        raw_path = os.environ.get(JOURNAL_ENV_VAR, "")
        if not raw_path:
            raise EraseJournalError(f"{JOURNAL_ENV_VAR} must be set")
        key = os.environ.get(KEY_ENV_VAR, "")
        return cls(Path(raw_path), key)

    @property
    def path(self) -> Path:
        return self._path

    def append_intent(self, *, request_id: str, target_type: str, target_id: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_CREAT | os.O_APPEND | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                self._recover_pending_locked(handle.fileno())
                payload = self._next_intent(
                    request_id=request_id, target_type=target_type, target_id=target_id
                )
                witness = self._pending_witness(payload)
                self._write_pending(witness)
                self._append_record_locked(handle, payload)
                self._write_head(payload)
                self._remove_pending()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def recover_pending(self) -> None:
        if not self._pending_path.exists():
            return
        if not self._path.exists():
            raise EraseJournalError("pending erase journal cannot recover missing journal")
        descriptor = os.open(self._path, os.O_APPEND | os.O_RDWR)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                self._recover_pending_locked(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _next_intent(self, *, request_id: str, target_type: str, target_id: str) -> dict[str, Any]:
        records = self.load() if self._path.exists() else []
        if any(record.request_id == request_id for record in records):
            raise EraseJournalError("erase journal request ID already exists")
        previous = records[-1] if records else None
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_type": "erase_intent",
            "seq": (previous.seq + 1) if previous is not None else 1,
            "request_id": request_id,
            "target_type": target_type,
            "target_id": target_id,
            "prev_digest": previous.digest if previous is not None else GENESIS_DIGEST,
        }
        digest = _digest(payload)
        payload["digest"] = digest
        payload["hmac"] = _signature(self._key, payload)
        _parse_payload(_canonical_json(payload), line_no=int(payload["seq"]))
        return payload

    def load(self) -> list[EraseJournalRecord]:
        if not self._path.exists():
            raise EraseJournalError(f"erase journal does not exist: {self._path}")
        if self._pending_path.exists():
            raise EraseJournalError("pending erase journal recovery is required")
        records = self._parse_journal_bytes(self._path.read_bytes())
        self._verify_head(records)
        return records

    @property
    def _head_path(self) -> Path:
        return self._path.with_name(self._path.name + ".head")

    @property
    def _pending_path(self) -> Path:
        return self._path.with_name(self._path.name + ".pending")

    def _append_record_locked(self, handle: Any, record: dict[str, Any]) -> None:
        handle.write(_record_line(record).decode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())

    def _write_head(self, record: dict[str, Any]) -> None:
        self._write_signed_head(_signed_head(self._key, record))

    def _write_signed_head(self, signed: dict[str, Any]) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._path.parent, delete=False
            ) as handle:
                temporary = handle.name
                handle.write(_canonical_json(signed))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._head_path)
            temporary = None
            _fsync_directory(self._path.parent)
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def _write_pending(self, witness: dict[str, Any]) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._path.parent, delete=False
            ) as handle:
                temporary = handle.name
                handle.write(_canonical_json(witness))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._pending_path)
            temporary = None
            _fsync_directory(self._path.parent)
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def _remove_pending(self) -> None:
        try:
            self._pending_path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(self._path.parent)

    def _pending_witness(self, next_record: dict[str, Any]) -> dict[str, Any]:
        journal_bytes = self._path.read_bytes() if self._path.exists() else b""
        pending = {
            "schema_version": PENDING_SCHEMA_VERSION,
            "old_head": self._read_signed_head(allow_missing=True),
            "old_journal_size": len(journal_bytes),
            "old_journal_digest": hashlib.sha256(journal_bytes).hexdigest(),
            "next_record": next_record,
            "expected_new_head": _signed_head(self._key, next_record),
        }
        return {**pending, "hmac": self._pending_signature(pending)}

    def _recover_pending_locked(self, journal_fd: int) -> None:
        if not self._pending_path.exists():
            return
        _verify_locked_journal_inode(journal_fd, self._path)
        witness = self._read_pending()
        journal_bytes = self._path.read_bytes() if self._path.exists() else b""
        old_size = int(witness["old_journal_size"])
        old_digest = str(witness["old_journal_digest"])
        next_line = _record_line(cast(dict[str, Any], witness["next_record"]))
        suffix = journal_bytes[old_size:]
        if (
            len(journal_bytes) < old_size
            or hashlib.sha256(journal_bytes[:old_size]).hexdigest() != old_digest
        ):
            raise EraseJournalError("pending erase journal old prefix mismatch")
        self._validate_pending_prefix(
            journal_bytes[:old_size],
            cast(dict[str, Any] | None, witness["old_head"]),
        )

        current_head = self._read_signed_head(allow_missing=True)
        old_head = witness["old_head"]
        expected_new_head = cast(dict[str, Any], witness["expected_new_head"])
        if suffix == next_line and current_head == expected_new_head:
            self._remove_pending()
            return
        if suffix == b"" and current_head == old_head:
            _write_all(journal_fd, next_line)
            os.fsync(journal_fd)
            self._write_signed_head(expected_new_head)
            self._remove_pending()
            return
        if suffix == next_line and current_head == old_head:
            self._write_signed_head(expected_new_head)
            self._remove_pending()
            return
        raise EraseJournalError("pending erase journal state is not recoverable")

    def _read_pending(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EraseJournalError("pending erase journal witness is invalid") from exc
        if not isinstance(loaded, dict):
            raise EraseJournalError("pending erase journal witness is invalid")
        witness = cast(dict[str, Any], loaded)
        required = {
            "schema_version",
            "old_head",
            "old_journal_size",
            "old_journal_digest",
            "next_record",
            "expected_new_head",
            "hmac",
        }
        if set(witness) != required or witness["schema_version"] != PENDING_SCHEMA_VERSION:
            raise EraseJournalError("pending erase journal witness is invalid")
        unsigned = {key: witness[key] for key in required if key != "hmac"}
        expected = self._pending_signature(unsigned)
        if not hmac.compare_digest(str(witness["hmac"]), expected):
            raise EraseJournalError("pending erase journal signature mismatch")
        if not isinstance(witness["old_journal_size"], int) or witness["old_journal_size"] < 0:
            raise EraseJournalError("pending erase journal old size is invalid")
        if not isinstance(witness["old_journal_digest"], str):
            raise EraseJournalError("pending erase journal old digest is invalid")
        old_head = witness["old_head"]
        if old_head is not None:
            self._verify_signed_head(cast(dict[str, Any], old_head))
        elif (
            witness["old_journal_size"] != 0
            or witness["old_journal_digest"] != hashlib.sha256(b"").hexdigest()
        ):
            raise EraseJournalError("genesis pending witness must bind an empty journal")
        next_record = cast(dict[str, Any], witness["next_record"])
        _parse_payload(_canonical_json(next_record), line_no=int(next_record.get("seq", 0)))
        expected_record_signature = _signature(self._key, next_record)
        if not hmac.compare_digest(str(next_record["hmac"]), expected_record_signature):
            raise EraseJournalError("pending erase journal record signature mismatch")
        expected_new_head = cast(dict[str, Any], witness["expected_new_head"])
        self._verify_signed_head(expected_new_head)
        old_head = witness["old_head"]
        expected_seq = 1 if old_head is None else int(cast(dict[str, Any], old_head)["seq"]) + 1
        expected_prev_digest = (
            GENESIS_DIGEST if old_head is None else str(cast(dict[str, Any], old_head)["digest"])
        )
        if next_record["seq"] != expected_seq or next_record["prev_digest"] != expected_prev_digest:
            raise EraseJournalError("pending erase journal record does not extend old head")
        expected_head = _signed_head(self._key, next_record)
        if expected_new_head != expected_head:
            raise EraseJournalError("pending erase journal expected head mismatch")
        return witness

    def _parse_journal_bytes(self, journal_bytes: bytes) -> list[EraseJournalRecord]:
        records: list[EraseJournalRecord] = []
        request_ids: set[str] = set()
        prev_digest = GENESIS_DIGEST
        for line_no, line_bytes in enumerate(journal_bytes.splitlines(keepends=True), start=1):
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EraseJournalError(f"erase journal line {line_no} is invalid UTF-8") from exc
            if not line_bytes.endswith(b"\n"):
                raise EraseJournalError(f"erase journal line {line_no} is incomplete")
            raw = line[:-1]
            if not raw.strip():
                raise EraseJournalError(f"erase journal line {line_no} is blank")
            payload = _parse_payload(raw, line_no=line_no)
            if _record_line(payload) != line_bytes:
                raise EraseJournalError(f"erase journal line {line_no} is not canonical")
            if payload["request_id"] in request_ids:
                raise EraseJournalError("erase journal request ID already exists")
            request_ids.add(str(payload["request_id"]))
            if payload["seq"] != len(records) + 1:
                raise EraseJournalError("erase journal sequence is not contiguous")
            if payload["prev_digest"] != prev_digest:
                raise EraseJournalError("erase journal hash chain is broken")
            digest = _digest(_unsigned_payload(payload))
            if payload["digest"] != digest:
                raise EraseJournalError("erase journal digest mismatch")
            expected = _signature(self._key, payload)
            if not hmac.compare_digest(str(payload["hmac"]), expected):
                raise EraseJournalError("erase journal signature mismatch")
            records.append(
                EraseJournalRecord(
                    seq=int(payload["seq"]),
                    request_id=str(payload["request_id"]),
                    target_type=str(payload["target_type"]),
                    target_id=str(payload["target_id"]),
                    prev_digest=str(payload["prev_digest"]),
                    digest=str(payload["digest"]),
                )
            )
            prev_digest = str(payload["digest"])
        return records

    def _validate_pending_prefix(
        self,
        prefix_bytes: bytes,
        old_head: dict[str, Any] | None,
    ) -> None:
        records = self._parse_journal_bytes(prefix_bytes)
        if old_head is None:
            if records:
                raise EraseJournalError("pending erase journal old prefix does not match old head")
            return
        if not records:
            raise EraseJournalError("pending erase journal old prefix does not match old head")
        if records[-1].seq != old_head["seq"] or records[-1].digest != old_head["digest"]:
            raise EraseJournalError("pending erase journal old prefix does not match old head")

    def _read_signed_head(self, *, allow_missing: bool) -> dict[str, Any] | None:
        try:
            loaded = json.loads(self._head_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise EraseJournalError("erase journal head missing or inconsistent") from exc
        if not isinstance(loaded, dict):
            raise EraseJournalError("erase journal head missing or inconsistent")
        signed = cast(dict[str, Any], loaded)
        self._verify_signed_head(signed)
        return signed

    def _verify_signed_head(self, signed: dict[str, Any]) -> None:
        if set(signed) != {"seq", "digest", "hmac"}:
            raise EraseJournalError("erase journal head missing or inconsistent")
        head = {"seq": signed["seq"], "digest": signed["digest"]}
        if not hmac.compare_digest(str(signed["hmac"]), self._head_signature(head)):
            raise EraseJournalError("erase journal head missing or inconsistent")

    def _pending_signature(self, pending: dict[str, Any]) -> str:
        payload = ("erase-journal-pending-v1:" + _canonical_json(pending)).encode("utf-8")
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def _verify_head(self, records: list[EraseJournalRecord]) -> None:
        if not records and not self._head_path.exists():
            return
        try:
            signed = json.loads(self._head_path.read_text(encoding="utf-8"))
            if not isinstance(signed, dict) or set(signed) != {"seq", "digest", "hmac"}:
                raise ValueError("invalid head fields")
            head = {"seq": signed["seq"], "digest": signed["digest"]}
            if not hmac.compare_digest(str(signed["hmac"]), self._head_signature(head)):
                raise ValueError("invalid head signature")
            if (
                not records
                or signed["seq"] != records[-1].seq
                or signed["digest"] != records[-1].digest
            ):
                raise ValueError("head does not match journal")
        except (OSError, ValueError, TypeError) as exc:
            raise EraseJournalError("erase journal head missing or inconsistent") from exc

    def _head_signature(self, head: dict[str, Any]) -> str:
        payload = ("erase-journal-head-v1:" + _canonical_json(head)).encode("utf-8")
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()


def _parse_payload(raw: str, *, line_no: int) -> dict[str, Any]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EraseJournalError(f"erase journal line {line_no} is invalid JSON") from exc
    if not isinstance(loaded, dict):
        raise EraseJournalError(f"erase journal line {line_no} must be a JSON object")
    payload = cast(dict[str, Any], loaded)
    required = {
        "schema_version",
        "event_type",
        "seq",
        "request_id",
        "target_type",
        "target_id",
        "prev_digest",
        "digest",
        "hmac",
    }
    if set(payload) != required:
        raise EraseJournalError(f"erase journal line {line_no} has invalid fields")
    if payload["schema_version"] != SCHEMA_VERSION or payload["event_type"] != "erase_intent":
        raise EraseJournalError(f"erase journal line {line_no} has invalid schema")
    if not isinstance(payload["seq"], int) or payload["seq"] <= 0:
        raise EraseJournalError(f"erase journal line {line_no} has invalid seq")
    for field in ("request_id", "target_type", "target_id", "prev_digest", "digest", "hmac"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise EraseJournalError(f"erase journal line {line_no} has invalid {field}")
    if payload["target_type"] not in {"knowledge_object", "memory_candidate", "formal_memory"}:
        raise EraseJournalError(f"erase journal line {line_no} has unsupported target type")
    return payload


def _unsigned_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "schema_version",
            "event_type",
            "seq",
            "request_id",
            "target_type",
            "target_id",
            "prev_digest",
        )
    }


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _signature(key: bytes, payload: dict[str, Any]) -> str:
    signed = {
        key: payload[key]
        for key in (
            "schema_version",
            "event_type",
            "seq",
            "request_id",
            "target_type",
            "target_id",
            "prev_digest",
            "digest",
        )
    }
    return hmac.new(key, _canonical_json(signed).encode("utf-8"), hashlib.sha256).hexdigest()


def _signed_head(key: bytes, record: dict[str, Any]) -> dict[str, Any]:
    head = {"seq": record["seq"], "digest": record["digest"]}
    payload = ("erase-journal-head-v1:" + _canonical_json(head)).encode("utf-8")
    return {**head, "hmac": hmac.new(key, payload, hashlib.sha256).hexdigest()}


def _record_line(record: dict[str, Any]) -> bytes:
    return (_canonical_json(record) + "\n").encode("utf-8")


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("failed to write erase journal record")
        remaining = remaining[written:]


def _verify_locked_journal_inode(descriptor: int, path: Path) -> None:
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = path.stat()
    except OSError as exc:
        raise EraseJournalError("pending erase journal path cannot be verified") from exc
    if descriptor_stat.st_dev != path_stat.st_dev or descriptor_stat.st_ino != path_stat.st_ino:
        raise EraseJournalError("pending erase journal locked file no longer matches path")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
