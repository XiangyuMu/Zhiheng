"""Crash boundary checks for strict load and append-time pending recovery."""

from pathlib import Path
from typing import Any

import pytest

from zhiheng.privacy.erase_journal import EraseJournalError, ExternalEraseJournal


@pytest.mark.parametrize("existing_records", [0, 1])
def test_durable_intent_with_unpublished_head_loads_strictly_then_append_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_records: int,
) -> None:
    path = tmp_path / "synthetic-erase-journal.jsonl"
    key = "synthetic-journal-crash-test-key"
    journal = ExternalEraseJournal(path, key)
    if existing_records:
        journal.append_intent(
            request_id="committed-intent",
            target_type="knowledge_object",
            target_id="object-1",
        )
    head = path.with_name(path.name + ".head")
    previous_head = head.read_bytes() if head.exists() else None

    def fail_head_publication(record: dict[str, Any]) -> None:
        # append_intent invokes this only after flushing/fsyncing the journal.
        assert record["seq"] == existing_records + 1
        raise OSError("synthetic crash after journal fsync, before head publish")

    monkeypatch.setattr(journal, "_write_head", fail_head_publication)
    with pytest.raises(OSError, match="after journal fsync"):
        journal.append_intent(
            request_id="interrupted-intent",
            target_type="knowledge_object",
            target_id="object-2",
        )
    durable_bytes = path.read_bytes()
    assert len(durable_bytes.splitlines()) == existing_records + 1
    assert b"interrupted-intent" in durable_bytes
    assert (head.read_bytes() if head.exists() else None) == previous_head

    restarted = ExternalEraseJournal(path, key)
    with pytest.raises(EraseJournalError, match="recovery is required"):
        restarted.load()
    restarted.append_intent(
        request_id="later-intent",
        target_type="knowledge_object",
        target_id="object-3",
    )
    records = restarted.load()
    assert [record.request_id for record in records] == [
        *(["committed-intent"] if existing_records else []),
        "interrupted-intent",
        "later-intent",
    ]
    assert path.read_bytes() != durable_bytes
    assert (head.read_bytes() if head.exists() else None) != previous_head
