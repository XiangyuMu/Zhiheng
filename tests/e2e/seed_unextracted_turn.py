"""Persist a conversation turn whose extraction failed, in the configured database.

The #117 browser stage needs a past turn holding judgments that were never
extracted. The live Worker extracts every turn it is given, so this stands in
for an extraction job that failed: it appends the turn exactly as the answer
API does, takes its outbox event so no job picks it up, and records the
failure through the same path the Worker uses. It touches nothing else and
prints the turn's history id.

    python tests/e2e/seed_unextracted_turn.py <conversation id> '<response json>' '<query>'
"""

from __future__ import annotations

import json
import sys

from sqlalchemy import text

from zhiheng.conclusions.extraction import ConversationConclusionDraftService
from zhiheng.core.config import get_settings
from zhiheng.db.session import create_session_factory, create_sqlite_engine, session_scope
from zhiheng.query.conversations import ConversationRepository


def main(argv: list[str]) -> int:
    conversation_id, response, query = argv[1], json.loads(argv[2]), argv[3]
    engine = create_sqlite_engine(get_settings())
    try:
        with session_scope(create_session_factory(engine)) as session:
            owner = str(
                session.execute(
                    text("SELECT owner_user_id FROM answer_conversations WHERE id = :id"),
                    {"id": conversation_id},
                ).scalar_one()
            )
            history_id = ConversationRepository().append(
                session,
                conversation_id=conversation_id,
                owner_user_id=owner,
                query=query,
                response=response,
            )
            session.execute(
                text("UPDATE outbox_events SET status = 'processed' WHERE id = :id"),
                {"id": f"conversation:{history_id}"},
            )
            ConversationConclusionDraftService().record_failure(
                session,
                history_id=history_id,
                conversation_id=conversation_id,
                owner_user_id=owner,
                query=query,
                answer=str(response.get("answer", "")),
                failure_code="EXTRACTION_FAILED",
                failure_reason="issue117 acceptance: the extraction job failed",
            )
    finally:
        engine.dispose()
    print(json.dumps({"history_id": history_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
