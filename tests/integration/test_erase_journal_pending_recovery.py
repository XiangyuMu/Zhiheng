from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from zhiheng.privacy import erase_journal as erase_journal_module
from zhiheng.privacy.erase_journal import EraseJournalError, ExternalEraseJournal, _record_line

JOURNAL_SECRET = "synthetic-pending-recovery-secret"


def _head_path(path: Path) -> Path:
    return path.with_name(path.name + ".head")


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + ".pending")


def _append_interrupted(
    journal: ExternalEraseJournal,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    if crash_point == "after_pending":
        def fail_append(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("synthetic crash after pending witness")

        monkeypatch.setattr(journal, "_append_record_locked", fail_append)
    elif crash_point == "after_journal":
        def fail_head(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("synthetic crash after journal append")

        monkeypatch.setattr(journal, "_write_head", fail_head)
    elif crash_point == "after_head":
        def fail_remove() -> None:
            raise OSError("synthetic crash after head publish")

        monkeypatch.setattr(journal, "_remove_pending", fail_remove)
    else:
        raise AssertionError(f"unsupported crash point: {crash_point}")

    with pytest.raises(OSError, match="synthetic crash"):
        journal.append_intent(
            request_id=f"request-{crash_point}",
            target_type="knowledge_object",
            target_id=f"object-{crash_point}",
        )


def _resign_pending(journal: ExternalEraseJournal, witness: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: value for key, value in witness.items() if key != "hmac"}
    return {**unsigned, "hmac": journal._pending_signature(unsigned)}


@pytest.mark.parametrize("existing_records", [0, 1])
@pytest.mark.parametrize("crash_point", ["after_pending", "after_journal", "after_head"])
def test_pending_recovery_completes_exact_crash_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_records: int,
    crash_point: str,
) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    for index in range(existing_records):
        journal.append_intent(
            request_id=f"committed-{index}",
            target_type="knowledge_object",
            target_id=f"object-{index}",
        )

    _append_interrupted(journal, monkeypatch, crash_point)
    assert _pending_path(path).exists()

    restarted = ExternalEraseJournal(path, JOURNAL_SECRET)
    restarted.recover_pending()
    records = restarted.load()

    assert [record.seq for record in records] == list(range(1, existing_records + 2))
    assert records[-1].request_id == f"request-{crash_point}"
    assert not _pending_path(path).exists()

    journal_bytes = path.read_bytes()
    head_bytes = _head_path(path).read_bytes()
    restarted.recover_pending()
    assert path.read_bytes() == journal_bytes
    assert _head_path(path).read_bytes() == head_bytes
    assert len(restarted.load()) == existing_records + 1


def test_recover_pending_refuses_to_create_missing_journal(tmp_path: Path) -> None:
    path = tmp_path / "erase-journal.jsonl"
    _pending_path(path).write_text("{}", encoding="utf-8")

    with pytest.raises(EraseJournalError, match="missing journal"):
        ExternalEraseJournal(path, JOURNAL_SECRET).recover_pending()

    assert not path.exists()


def test_load_rejects_blank_or_whitespace_journal_lines(tmp_path: Path) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    journal.append_intent(
        request_id="request-1",
        target_type="knowledge_object",
        target_id="object-1",
    )
    path.write_bytes(path.read_bytes() + b"   \n")

    with pytest.raises(EraseJournalError, match="line 2 is blank"):
        journal.load()


@pytest.mark.parametrize("existing_records", [0, 1])
def test_load_refuses_valid_journal_prefix_while_pending_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_records: int,
) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    for index in range(existing_records):
        journal.append_intent(
            request_id=f"committed-{index}",
            target_type="knowledge_object",
            target_id=f"object-{index}",
        )
    _append_interrupted(journal, monkeypatch, "after_pending")

    with pytest.raises(EraseJournalError, match="recovery is required"):
        ExternalEraseJournal(path, JOURNAL_SECRET).load()


def test_resigned_genesis_pending_rejects_nonempty_garbage_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    _append_interrupted(journal, monkeypatch, "after_pending")
    witness = json.loads(_pending_path(path).read_text(encoding="utf-8"))
    path.write_bytes(b"garbage-prefix" + path.read_bytes())
    _pending_path(path).write_text(
        json.dumps(_resign_pending(journal, witness), sort_keys=True),
        encoding="utf-8",
    )
    before_journal = path.read_bytes()
    before_pending = _pending_path(path).read_bytes()

    with pytest.raises(EraseJournalError, match="not recoverable"):
        ExternalEraseJournal(path, JOURNAL_SECRET).recover_pending()

    assert path.read_bytes() == before_journal
    assert _pending_path(path).read_bytes() == before_pending


def test_pending_recovery_uses_write_all_for_missing_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    _append_interrupted(journal, monkeypatch, "after_pending")
    real_write = os.write
    writes: list[int] = []

    def partial_write(descriptor: int, data: Any) -> int:
        payload = bytes(data)
        chunk_size = max(1, len(payload) // 2)
        writes.append(chunk_size)
        return int(real_write(descriptor, payload[:chunk_size]))

    monkeypatch.setattr(os, "write", partial_write)
    ExternalEraseJournal(path, JOURNAL_SECRET).recover_pending()

    assert len(writes) > 1
    recovered = ExternalEraseJournal(path, JOURNAL_SECRET).load()
    assert recovered[0].request_id == "request-after_pending"


def test_pending_next_record_must_extend_old_head_even_when_resigned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    journal.append_intent(
        request_id="committed",
        target_type="knowledge_object",
        target_id="object-1",
    )
    _append_interrupted(journal, monkeypatch, "after_pending")
    witness = json.loads(_pending_path(path).read_text(encoding="utf-8"))
    witness["next_record"]["seq"] = 1
    witness["next_record"]["digest"] = erase_journal_module._digest(
        erase_journal_module._unsigned_payload(witness["next_record"])
    )
    witness["next_record"]["hmac"] = erase_journal_module._signature(
        JOURNAL_SECRET.encode("utf-8"),
        witness["next_record"],
    )
    witness["expected_new_head"] = erase_journal_module._signed_head(
        JOURNAL_SECRET.encode("utf-8"),
        witness["next_record"],
    )
    _pending_path(path).write_text(
        json.dumps(_resign_pending(journal, witness), sort_keys=True),
        encoding="utf-8",
    )
    before = _pending_path(path).read_bytes()

    with pytest.raises(EraseJournalError, match="does not extend old head"):
        ExternalEraseJournal(path, JOURNAL_SECRET).recover_pending()

    assert _pending_path(path).read_bytes() == before


def test_resigned_pending_rejects_malformed_existing_prefix_without_rewriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    journal.append_intent(
        request_id="committed",
        target_type="knowledge_object",
        target_id="object-1",
    )
    _append_interrupted(journal, monkeypatch, "after_pending")
    witness = json.loads(_pending_path(path).read_text(encoding="utf-8"))
    lines = path.read_bytes().splitlines(keepends=True)
    lines[0] = b"[" + lines[0][1:]
    path.write_bytes(b"".join(lines))
    witness["old_journal_digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _pending_path(path).write_text(
        json.dumps(_resign_pending(journal, witness), sort_keys=True),
        encoding="utf-8",
    )
    before_journal = path.read_bytes()
    before_pending = _pending_path(path).read_bytes()

    with pytest.raises(EraseJournalError, match="invalid JSON"):
        ExternalEraseJournal(path, JOURNAL_SECRET).recover_pending()

    assert path.read_bytes() == before_journal
    assert _pending_path(path).read_bytes() == before_pending


def test_signed_prefix_truncation_without_pending_still_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    for index in range(2):
        journal.append_intent(
            request_id=f"request-{index}",
            target_type="knowledge_object",
            target_id=f"object-{index}",
        )

    first_line = path.read_bytes().splitlines(keepends=True)[0]
    path.write_bytes(first_line)
    truncated = path.read_bytes()
    head = _head_path(path).read_bytes()

    with pytest.raises(EraseJournalError, match="head missing or inconsistent"):
        journal.load()
    journal.recover_pending()
    assert path.read_bytes() == truncated
    assert _head_path(path).read_bytes() == head


@pytest.mark.parametrize(
    ("case_name", "mutate"),
    [
        ("partial_suffix", lambda path, witness: path.write_bytes(path.read_bytes() + b"{")),
        (
            "extra_suffix",
            lambda path, witness: path.write_bytes(
                path.read_bytes() + _record_line(witness["next_record"]) + b"{}\n"
            ),
        ),
        (
            "wrong_head",
            lambda path, witness: _head_path(path).write_text("{}", encoding="utf-8"),
        ),
        (
            "tampered_pending",
            lambda path, witness: _pending_path(path).write_text(
                _pending_path(path).read_text(encoding="utf-8").replace(
                    "request-after_pending",
                    "request-tampered",
                ),
                encoding="utf-8",
            ),
        ),
        ("malformed_pending", lambda path, witness: _pending_path(path).write_text("{")),
    ],
)
def test_pending_recovery_rejects_unproven_states_without_rewriting_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    mutate: Callable[[Path, dict[str, Any]], object],
) -> None:
    del case_name
    path = tmp_path / "erase-journal.jsonl"
    journal = ExternalEraseJournal(path, JOURNAL_SECRET)
    _append_interrupted(journal, monkeypatch, "after_pending")
    witness = json.loads(_pending_path(path).read_text(encoding="utf-8"))
    mutate(path, witness)
    journal_bytes = path.read_bytes()
    head_bytes = _head_path(path).read_bytes() if _head_path(path).exists() else None
    pending_bytes = _pending_path(path).read_bytes()

    with pytest.raises(EraseJournalError):
        ExternalEraseJournal(path, JOURNAL_SECRET).recover_pending()

    assert path.read_bytes() == journal_bytes
    assert (_head_path(path).read_bytes() if _head_path(path).exists() else None) == head_bytes
    assert _pending_path(path).read_bytes() == pending_bytes
