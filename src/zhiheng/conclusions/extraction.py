"""Deterministic extraction of reviewable conclusion drafts from conversations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

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

    _CONCLUSION_PATTERN = re.compile(
        r"(?:^|[。！？!?\n])\s*(?:(?:结论\s*\d*|conclusion\s*\d*)\s*[:：]|"
        r"(?:我认为|因此|所以|建议|i think|therefore)\s*)"
        r"([^。！？!?\n]+[。！？!?]?)",
        re.IGNORECASE,
    )
    _PLAIN_PATTERN = re.compile(
        r"(?:^|[。！？!?\n])\s*([^。！？!?\n]*(?:能|应该|可以|适合|有助于|意味着|需要|重要|最好|必须|会)"
        r"[^。！？!?\n]*[。！？!?]?)"
    )
    _PREMISE_PATTERN = re.compile(r"(?:^|\n)\s*前提\s*[:：]\s*([^。！？!?\n]+[。！？!?]?)")

    def extract(self, *, query: str, answer: str) -> list[ExtractedConclusion]:
        source = query.strip()
        if answer.strip():
            source = f"{source}\n助手：{answer.strip()}"
        premise_matches = list(self._PREMISE_PATTERN.finditer(source))
        explicit_matches = list(self._CONCLUSION_PATTERN.finditer(source))
        occupied = [(match.start(1), match.end(1)) for match in explicit_matches]
        plain_matches = [
            match
            for match in self._PLAIN_PATTERN.finditer(source)
            if not any(start < match.end(1) and match.start(1) < end for start, end in occupied)
        ]
        matches = sorted(
            [(match, True) for match in explicit_matches]
            + [(match, False) for match in plain_matches],
            key=lambda item: item[0].start(1),
        )
        found: list[ExtractedConclusion] = []
        seen: set[str] = set()
        premise_by_match: dict[int, list[dict[str, Any]]] = {
            index: [] for index in range(len(matches))
        }
        for premise_match in premise_matches:
            preceding = [
                index
                for index, (match, _) in enumerate(matches)
                if match.end(1) <= premise_match.start(1)
            ]
            target = preceding[-1] if preceding else (0 if matches else None)
            if target is not None:
                premise_by_match[target].append(
                    {"text": premise_match.group(1).strip(), "confirmed": False}
                )
        for index, (match, is_explicit) in enumerate(matches):
            claim = match.group(1).strip()
            if not claim or claim in seen:
                continue
            seen.add(claim)
            if is_explicit and claim.startswith("如果") and "，则" in claim:
                premise_text, claim = claim[2:].split("，则", 1)
                claim = claim.strip()
                item_premises = [{"text": premise_text.strip(), "confirmed": False}]
            else:
                item_premises = premise_by_match[index]
            found.append(
                ExtractedConclusion(
                    claim=claim,
                    premises=item_premises,
                    domain_id=_domain_for(claim),
                    excerpt=match.group(0).strip(),
                    evidence=[
                        {
                            "excerpt": match.group(0).strip(),
                            "source": "conversation",
                            "start_offset": match.start(1),
                            "end_offset": match.end(1),
                        }
                    ],
                    start_offset=match.start(1),
                    end_offset=match.end(1),
                )
            )
        return found


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
        if not extracted:
            return 0
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
        return created


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
