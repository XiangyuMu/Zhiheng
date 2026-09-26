"""Deterministic extraction of reviewable conclusion drafts from conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.conclusions.repository import ConclusionRepository
from zhiheng.core.ids import sha256_text


@dataclass(frozen=True, slots=True)
class ExtractedConclusion:
    claim: str
    premises: list[dict[str, Any]]
    domain_id: str
    excerpt: str
    evidence: list[dict[str, Any]]
    start_offset: int
    end_offset: int


class HeuristicConversationConclusionExtractor:
    """Extract explicitly marked conclusions without treating ordinary prose as fact."""

    _CLAUSE_END = "。！？!?\n"
    _PREMISE_PATTERN = re.compile(
        r"(?:^|[。\n])\s*(?:前提|条件|适用前提)\s*\d*\s*[:：]\s*([^。！？!?\n]+[。！？!?]?)"
    )
    _EXPLICIT_MARKER_PATTERN = re.compile(
        r"(?:结论|判断|建议|conclusion)\s*\d*\s*[:：]\s*([^。！？!?\n]+[。！？!?]?)",
        re.IGNORECASE,
    )
    _OPINION_PATTERN = re.compile(
        r"(?:^|[。！？!?\n])\s*(?:我认为|我的结论是|因此|所以|therefore|i think)\s*"
        r"([^。！？!?\n]+[。！？!?]?)",
        re.IGNORECASE,
    )
    _PLAIN_PATTERN = re.compile(
        r"(?:^|[。！？!?\n])\s*([^。！？!?\n]*(?:能|应该|可以|适合|有助于|意味着|需要|重要|最好|必须|会)"
        r"[^。！？!?\n]*[。！？!?]?)"
    )
    _IF_THEN_PATTERN = re.compile(
        r"(?:^|[。！？!?\n])\s*如果([^。！？!?\n，则]{1,120}(?:[^。！？!?\n，则])?)"
        r"[，,]\s*则([^。！？!?\n]+[。！？!?]?)"
    )
    _BECAUSE_SO_PATTERN = re.compile(
        r"(?:^|[。！？!?\n])\s*因为([^。！？!?\n，,]{1,120}(?:[^。！？!?\n，,])?)"
        r"[，,]\s*所以([^。！？!?\n]+[。！？!?]?)"
    )
    _IN_PREMISE_PATTERN = re.compile(
        r"(?:在|当)([^。！？!?\n，,]{1,120}(?:[^。！？!?\n，,])?)"
        r"(?:的条件下|的前提下)[，,]\s*(?:结论|判断|建议)?\s*[:：]?\s*"
        r"([^。！？!?\n]+[。！？!?]?)"
    )
    _NEGATIVE_CONTEXT_PATTERN = re.compile(
        r"(?:引用|原文|他说|她说|别人说|玩笑|开玩笑|笑话|反话|假设一下|假如只是|"
        r"只是举例|并不代表我|不是我的结论|不要记录|不要当真|quote|joke|hypothetical)",
        re.IGNORECASE,
    )
    _CLAIM_START_BLOCKLIST = (
        "有人说",
        "他说",
        "她说",
        "别人说",
        "引用",
        "原文",
        "假设",
        "假如",
        "开玩笑",
        "玩笑",
    )
    _CLAIM_CONTENT_BLOCKLIST = ("不要记录", "不要当真", "不是我的结论")

    def extract(self, *, query: str, answer: str) -> list[ExtractedConclusion]:
        source = query.strip()
        if answer.strip():
            source = f"{source}\n助手：{answer.strip()}"
        matches = sorted(self._candidate_matches(source), key=lambda item: item.claim_start)
        found: list[ExtractedConclusion] = []
        seen: set[str] = set()
        premise_matches = list(self._PREMISE_PATTERN.finditer(source))
        premise_by_candidate = _assign_premises(premise_matches, matches)
        for candidate_index, candidate in enumerate(matches):
            claim = _clean_clause(candidate.claim)
            if not claim or claim in seen or _is_blocked_claim(claim):
                continue
            seen.add(claim)
            item_premises = [
                {"text": _clean_clause(text), "confirmed": False}
                for text in candidate.inline_premises
                if _clean_clause(text)
            ]
            item_premises.extend(
                {"text": _clean_clause(match.group(1)), "confirmed": False}
                for match in premise_by_candidate.get(candidate_index, ())
                if _clean_clause(match.group(1))
            )
            found.append(
                ExtractedConclusion(
                    claim=claim,
                    premises=item_premises,
                    domain_id=_domain_for(claim),
                    excerpt=source[candidate.excerpt_start : candidate.excerpt_end].strip(),
                    evidence=[
                        {
                            "excerpt": source[
                                candidate.excerpt_start : candidate.excerpt_end
                            ].strip(),
                            "source": "conversation",
                            "start_offset": candidate.claim_start,
                            "end_offset": candidate.claim_end,
                        }
                    ],
                    start_offset=candidate.claim_start,
                    end_offset=candidate.claim_end,
                )
            )
        return found

    def unrecognized_review_items(self, *, query: str, answer: str) -> list[dict[str, Any]]:
        """Return explicitly marked expressions rejected by safety filters."""
        source = query.strip()
        if answer.strip():
            source = f"{source}\n助手：{answer.strip()}"
        candidates = self._candidate_matches(source, filter_negative=False)
        extracted_claims = {item.claim for item in self.extract(query=query, answer=answer)}
        items: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for candidate in candidates:
            if candidate.claim in extracted_claims or (
                candidate.claim_start,
                candidate.claim_end,
            ) in seen:
                continue
            if not _has_negative_context(source, candidate):
                continue
            seen.add((candidate.claim_start, candidate.claim_end))
            items.append(
                {
                    "excerpt": source[candidate.excerpt_start : candidate.excerpt_end].strip(),
                    "start_offset": candidate.claim_start,
                    "end_offset": candidate.claim_end,
                    "reason_code": "rejected_context",
                    "reason": "表达处于引用、玩笑或假设语境，未自动写入正式记忆；请人工确认。",
                }
            )
        return items

    def _candidate_matches(
        self, source: str, *, filter_negative: bool = True
    ) -> list[_CandidateMatch]:
        candidates: list[_CandidateMatch] = []
        for pattern in (self._EXPLICIT_MARKER_PATTERN, self._OPINION_PATTERN):
            for match in pattern.finditer(source):
                candidates.append(
                    _CandidateMatch(
                        claim=match.group(1),
                        claim_start=match.start(1),
                        claim_end=match.end(1),
                        excerpt_start=match.start(),
                        excerpt_end=match.end(),
                        inline_premises=[],
                    )
                )
        occupied = [(candidate.claim_start, candidate.claim_end) for candidate in candidates]
        for match in self._PLAIN_PATTERN.finditer(source):
            if any(start < match.end(1) and match.start(1) < end for start, end in occupied):
                continue
            claim = match.group(1).strip()
            if claim.startswith(("前提", "条件", "引用", "原文")):
                continue
            candidates.append(
                _CandidateMatch(
                    claim=claim,
                    claim_start=match.start(1),
                    claim_end=match.end(1),
                    excerpt_start=match.start(),
                    excerpt_end=match.end(),
                    inline_premises=[],
                )
            )
        for pattern in (
            self._IF_THEN_PATTERN,
            self._BECAUSE_SO_PATTERN,
            self._IN_PREMISE_PATTERN,
        ):
            for match in pattern.finditer(source):
                candidates.append(
                    _CandidateMatch(
                        claim=match.group(2),
                        claim_start=match.start(2),
                        claim_end=match.end(2),
                        excerpt_start=match.start(),
                        excerpt_end=match.end(),
                        inline_premises=[match.group(1)],
                    )
                )
        return (
            [candidate for candidate in candidates if not _has_negative_context(source, candidate)]
            if filter_negative
            else candidates
        )


@dataclass(frozen=True, slots=True)
class _CandidateMatch:
    claim: str
    claim_start: int
    claim_end: int
    excerpt_start: int
    excerpt_end: int
    inline_premises: list[str]


def _assign_premises(
    premise_matches: list[re.Match[str]], candidates: list[_CandidateMatch]
) -> dict[int, list[re.Match[str]]]:
    """Attach each explicit premise to the nearest claim exactly once."""
    assigned: dict[int, list[re.Match[str]]] = {}
    for premise in premise_matches:
        premise_start, premise_end = premise.start(1), premise.end(1)
        previous = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if candidate.claim_end <= premise_start
        ]
        following = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if candidate.claim_start >= premise_end
        ]
        previous_item = previous[-1] if previous else None
        following_item = following[0] if following else None
        if previous_item is None and following_item is None:
            continue
        if previous_item is None:
            target = following_item
        elif following_item is None:
            target = previous_item
        else:
            previous_distance = premise_start - previous_item[1].claim_end
            following_distance = following_item[1].claim_start - premise_end
            target = previous_item if previous_distance <= following_distance else following_item
        assert target is not None
        assigned.setdefault(target[0], []).append(premise)
    return assigned


def _has_negative_context(source: str, candidate: _CandidateMatch) -> bool:
    start = _sentence_start(source, candidate.excerpt_start)
    end = _sentence_end(source, candidate.excerpt_end)
    context = source[start:end]
    return bool(HeuristicConversationConclusionExtractor._NEGATIVE_CONTEXT_PATTERN.search(context))


def _sentence_start(source: str, offset: int) -> int:
    boundary = max(source.rfind(char, 0, offset) for char in "。！？!?\n")
    return 0 if boundary < 0 else boundary + 1


def _sentence_end(source: str, offset: int) -> int:
    endings = [position for char in "。！？!?\n" if (position := source.find(char, offset)) >= 0]
    return len(source) if not endings else min(endings) + 1


def _clean_clause(text: str) -> str:
    return text.strip(" \t\r\n：:，,；;\"'“”‘’")


def _is_blocked_claim(claim: str) -> bool:
    return claim.startswith(HeuristicConversationConclusionExtractor._CLAIM_START_BLOCKLIST) or any(
        blocked in claim
        for blocked in HeuristicConversationConclusionExtractor._CLAIM_CONTENT_BLOCKLIST
    )


class ConversationConclusionDraftService:
    def __init__(
        self,
        repository: ConclusionRepository | None = None,
        extractor: HeuristicConversationConclusionExtractor | None = None,
    ) -> None:
        self.repository = repository or ConclusionRepository()
        self.extractor = extractor or HeuristicConversationConclusionExtractor()

    def extract_and_persist(
        self,
        session: Session,
        *,
        history_id: str,
        conversation_id: str,
        owner_user_id: str,
        query: str,
        answer: str,
    ) -> int:
        extracted = self.extractor.extract(query=query, answer=answer)
        review_items = self.extractor.unrecognized_review_items(query=query, answer=answer)
        source_text = query.strip()
        if answer.strip():
            source_text = f"{source_text}\n助手：{answer.strip()}"
        source = self.repository.persist_source(
            session,
            owner_user_id,
            source_text,
            f"conclusion-source:{history_id}",
            history_id=history_id,
        )
        created = 0
        for index, item in enumerate(extracted, start=1):
            claim_hash = sha256_text(item.claim)
            self.repository.create_draft(
                session,
                owner_user_id,
                str(source["id"]),
                {
                    "title": f"对话结论：{item.claim[:80]}",
                    "claim": item.claim,
                    "domain_id": item.domain_id,
                    "premises": item.premises,
                    "excerpt": item.excerpt,
                    "evidence": [
                        {
                            **evidence,
                            "conversation_id": conversation_id,
                            "history_id": history_id,
                            "start_offset": item.start_offset,
                            "end_offset": item.end_offset,
                        }
                        for evidence in item.evidence
                    ],
                },
                f"conclusion-draft:{history_id}:{index}:{claim_hash}",
            )
            created += 1
        self._record_run(
            session,
            history_id=history_id,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            source_id=str(source["id"]),
            source_text=source_text,
            extracted_count=created,
            review_items=review_items,
        )
        return created

    def record_failure(
        self,
        session: Session,
        *,
        history_id: str,
        conversation_id: str,
        owner_user_id: str,
        query: str,
        answer: str,
        failure_code: str,
        failure_reason: str,
    ) -> None:
        source_text = query.strip()
        if answer.strip():
            source_text = f"{source_text}\n助手：{answer.strip()}"
        if not source_text:
            source_text = "原始对话不可用。"
        source = self.repository.persist_source(
            session,
            owner_user_id,
            source_text,
            f"conclusion-source:{history_id}",
            history_id=history_id,
        )
        self._record_run(
            session,
            history_id=history_id,
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            source_id=str(source["id"]),
            source_text=source_text,
            extracted_count=0,
            status="failed",
            failure_code=failure_code,
            failure_reason=failure_reason,
            review_items=[],
        )

    def _record_run(
        self,
        session: Session,
        *,
        history_id: str,
        conversation_id: str,
        owner_user_id: str,
        source_id: str,
        source_text: str,
        extracted_count: int,
        status: str = "succeeded",
        failure_code: str | None = None,
        failure_reason: str | None = None,
        review_items: list[dict[str, Any]] | None = None,
    ) -> None:
        run_id = f"conversation-extraction:{history_id}"
        review_items = list(review_items or [])
        needs_supplement = status == "failed" or extracted_count == 0 or bool(review_items)
        unrecognized_count = len(review_items) if review_items else (1 if needs_supplement else 0)
        session.execute(
            text(
                """
                INSERT INTO conversation_extraction_runs (
                  id, owner_user_id, conversation_id, history_id, source_id, status,
                  extracted_count, unrecognized_count, failure_code, failure_reason,
                  extractor_version
                )
                VALUES (
                  :id, :owner, :conversation, :history, :source, :status,
                  :extracted_count, :unrecognized_count, :failure_code, :failure_reason,
                  'heuristic-conclusion-v1'
                )
                ON CONFLICT(history_id) DO UPDATE SET
                  source_id=excluded.source_id,
                  status=excluded.status,
                  extracted_count=excluded.extracted_count,
                  unrecognized_count=excluded.unrecognized_count,
                  failure_code=excluded.failure_code,
                  failure_reason=excluded.failure_reason,
                  updated_at=CURRENT_TIMESTAMP
                """
            ),
            {
                "id": run_id,
                "owner": owner_user_id,
                "conversation": conversation_id,
                "history": history_id,
                "source": source_id,
                "status": status,
                "extracted_count": extracted_count,
                "unrecognized_count": unrecognized_count,
                "failure_code": failure_code,
                "failure_reason": failure_reason,
            },
        )
        session.execute(
            text("DELETE FROM conversation_extraction_review_items WHERE run_id=:run_id"),
            {"run_id": run_id},
        )
        if not needs_supplement:
            return
        reason_code = "extraction_failed" if status == "failed" else "no_supported_expression"
        reason = (
            failure_reason
            if status == "failed" and failure_reason
            else "实际提炼器没有识别出可直接生成草稿的结论；请从原文手动补充。"
        )
        if not review_items:
            review_items = [
                {
                    "excerpt": source_text,
                    "start_offset": 0,
                    "end_offset": len(source_text),
                    "reason_code": reason_code,
                    "reason": reason[:1000],
                }
            ]
        for index, item in enumerate(review_items):
            session.execute(
                text(
                    """
                    INSERT INTO conversation_extraction_review_items (
                      id, run_id, owner_user_id, kind, excerpt, start_offset, end_offset,
                      reason_code, reason
                    )
                    VALUES (:id, :run_id, :owner, :kind, :excerpt, :start_offset,
                            :end_offset, :reason_code, :reason)
                    """
                ),
                {
                    "id": f"{run_id}:review:{index}",
                    "run_id": run_id,
                    "owner": owner_user_id,
                    "kind": "failure" if status == "failed" else "manual_supplement",
                    "excerpt": str(item["excerpt"]),
                    "start_offset": int(item["start_offset"]),
                    "end_offset": int(item["end_offset"]),
                    "reason_code": str(item["reason_code"]),
                    "reason": str(item["reason"])[:1000],
                },
            )


def list_extraction_review_results(
    session: Session, owner_user_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT r.*, s.body AS source_text,
                   COUNT(e.id) AS draft_count
            FROM conversation_extraction_runs r
            JOIN conclusion_sources s
              ON s.id=r.source_id AND s.owner_user_id=r.owner_user_id
            LEFT JOIN conclusion_entries e
              ON e.source_id=r.source_id
             AND e.owner_user_id=r.owner_user_id
             AND e.status IN ('draft','deferred','formal','merged','superseded')
            WHERE r.owner_user_id=:owner
              AND (r.unrecognized_count > 0 OR r.status='failed')
            GROUP BY r.id
            ORDER BY datetime(r.updated_at) DESC, r.id DESC
            LIMIT :limit
            """
        ),
        {"owner": owner_user_id, "limit": max(1, min(limit, 500))},
    ).mappings()
    out: list[dict[str, Any]] = []
    for row in rows:
        items = session.execute(
            text(
                """
                SELECT id, kind, excerpt, start_offset, end_offset, reason_code, reason
                FROM conversation_extraction_review_items
                WHERE run_id=:run_id AND owner_user_id=:owner
                ORDER BY id ASC
                """
            ),
            {"run_id": row["id"], "owner": owner_user_id},
        ).mappings()
        out.append(
            {
                "id": row["id"],
                "status": row["status"],
                "conversation_id": row["conversation_id"],
                "history_id": row["history_id"],
                "source": {"id": row["source_id"], "text": row["source_text"]},
                "extracted_count": row["extracted_count"],
                "unrecognized_count": row["unrecognized_count"],
                "draft_count": row["draft_count"],
                "failure_code": row["failure_code"],
                "failure_reason": row["failure_reason"],
                "extractor_version": row["extractor_version"],
                "updated_at": row["updated_at"],
                "review_items": [dict(item) for item in items],
            }
        )
    return out


def get_extraction_review_result(
    session: Session, owner_user_id: str, run_id: str
) -> dict[str, Any] | None:
    """Return one owner-scoped extraction run, including its source and review items."""
    row = (
        session.execute(
            text(
                """
                SELECT r.*, s.body AS source_text,
                       COUNT(e.id) AS draft_count
                FROM conversation_extraction_runs r
                JOIN conclusion_sources s
                  ON s.id=r.source_id AND s.owner_user_id=r.owner_user_id
                LEFT JOIN conclusion_entries e
                  ON e.source_id=r.source_id
                 AND e.owner_user_id=r.owner_user_id
                 AND e.status IN ('draft','deferred','formal','merged','superseded')
                WHERE r.owner_user_id=:owner AND r.id=:run_id
                GROUP BY r.id
                """
            ),
            {"owner": owner_user_id, "run_id": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    items = session.execute(
        text(
            """
            SELECT id, kind, excerpt, start_offset, end_offset, reason_code, reason
            FROM conversation_extraction_review_items
            WHERE run_id=:run_id AND owner_user_id=:owner
            ORDER BY id ASC
            """
        ),
        {"run_id": run_id, "owner": owner_user_id},
    ).mappings()
    return {
        "id": row["id"],
        "status": row["status"],
        "conversation_id": row["conversation_id"],
        "history_id": row["history_id"],
        "source": {"id": row["source_id"], "text": row["source_text"]},
        "extracted_count": row["extracted_count"],
        "unrecognized_count": row["unrecognized_count"],
        "draft_count": row["draft_count"],
        "failure_code": row["failure_code"],
        "failure_reason": row["failure_reason"],
        "extractor_version": row["extractor_version"],
        "updated_at": row["updated_at"],
        "review_items": [dict(item) for item in items],
    }


def _domain_for(claim: str) -> str:
    lowered = claim.lower()
    keyword_domains = (
        (("学习", "练习", "教育", "记忆"), "education_learning"),
        (("软件", "代码", "ai", "计算机", "技术"), "computing_engineering"),
        (("健康", "医学", "睡眠", "营养"), "medicine_health"),
        (("工作", "职业", "求职", "团队"), "career_work_practice"),
        (("沟通", "关系", "家庭", "冲突"), "relationships_communication"),
        (("钱", "投资", "商业", "金融"), "economics_finance_business"),
    )
    for keywords, domain_id in keyword_domains:
        if any(keyword in lowered for keyword in keywords):
            return domain_id
    return "education_learning"
