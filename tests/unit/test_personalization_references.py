from __future__ import annotations

import pytest
from fastapi import HTTPException

from zhiheng.api.retrieval import _validate_replayed_personalization_refs
from zhiheng.memory.context import MemoryContextEntry, MemoryContextSnapshot


def _snapshot() -> MemoryContextSnapshot:
    entry = MemoryContextEntry(
        formal_memory_id="memory-1",
        formal_version_id="version-1",
        confirmation_generation=3,
        state_key="goal.finance",
        layer="L0",
        value_json='{"text":"learn"}',
        memory_type="goal",
        origin_kind="explicit",
        source_kind="user_confirmed",
        sensitivity_level="private",
        confidence=1.0,
        valid_from="2025-01-01T00:00:00+00:00",
        valid_to=None,
    )
    return MemoryContextSnapshot(
        query_hash="query",
        topic_prefix=None,
        entries=(entry,),
        digest="digest",
        truncated=False,
        omitted_count=0,
        eligible_count=1,
        source_digest="source",
    )


def test_replayed_refs_must_match_current_formal_context() -> None:
    snapshot = _snapshot()
    _validate_replayed_personalization_refs(
        [
            {
                "formal_memory_id": "memory-1",
                "formal_version_id": "version-1",
                "confirmation_generation": 3,
                "state_key": "goal.finance",
            }
        ],
        snapshot,
    )

    with pytest.raises(HTTPException, match="references changed"):
        _validate_replayed_personalization_refs(
            [
                {
                    "formal_memory_id": "memory-1",
                    "formal_version_id": "version-old",
                    "confirmation_generation": 2,
                    "state_key": "goal.finance",
                }
            ],
            snapshot,
        )


def test_replayed_refs_reject_non_list_or_malformed_entries() -> None:
    snapshot = _snapshot()
    with pytest.raises(HTTPException, match="references are invalid"):
        _validate_replayed_personalization_refs({}, snapshot)
    with pytest.raises(HTTPException, match="references are invalid"):
        _validate_replayed_personalization_refs(["memory-1"], snapshot)
