"""The fixed five-question first-use questionnaire (#114, spec decision 25).

The questionnaire is offered once. Skipping it as a whole means it is never
shown again on its own; it can still be filled in later from 个人上下文, and
nothing nags the user to complete the profile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.classification.domain_keywords import match_primary_domains
from zhiheng.core.ids import json_text
from zhiheng.memory.personal_updates import PersonalUpdate, PersonalUpdateService
from zhiheng.memory.personalization import (
    PersonalizationSettings,
    ordered_primary_domains,
    primary_domain_name,
)

OnboardingStatus = Literal["not_started", "skipped", "completed"]

QUESTIONS: tuple[dict[str, str], ...] = (
    {"id": "occupation", "kind": "text", "prompt": "你的职业或所在领域是什么？"},
    {"id": "interests", "kind": "text", "prompt": "你主要对哪些领域感兴趣？"},
    {"id": "current_goal", "kind": "text", "prompt": "你当前的主要目标是什么？"},
    {"id": "answer_style", "kind": "text", "prompt": "你希望回答采用什么风格？"},
    {
        "id": "excluded_domains",
        "kind": "domains",
        "prompt": "哪些主领域的问题不希望使用你的个人信息？",
    },
)


@dataclass(frozen=True)
class ExplicitAnswer:
    """Where a free-text answer is remembered. A current goal is volatile, so it
    is re-checked when it is used again (spec decision 12)."""

    memory_type: str
    state_key: str
    time_sensitivity: str = "persistent"


EXPLICIT_ANSWERS: dict[str, ExplicitAnswer] = {
    "occupation": ExplicitAnswer("fact", "role.current"),
    "interests": ExplicitAnswer("preference", "interest.primary"),
    "current_goal": ExplicitAnswer("goal", "goal.personal", time_sensitivity="volatile"),
    "answer_style": ExplicitAnswer("preference", "preference.response_style"),
}

# Primary domains the system reads into an answer. They are inferences, so
# they only become memory once the user confirms them.
INFERRED_DOMAINS: dict[str, tuple[str, str]] = {
    "occupation": ("inference.work_domain", "从你的职业或领域推断，你的工作可能属于"),
    "interests": ("inference.interest_domains", "从你的兴趣推断，你主要关注"),
}


class OnboardingAlreadyCompletedError(ValueError):
    pass


class OnboardingQuestionnaire:
    def __init__(
        self,
        personal_updates: PersonalUpdateService | None = None,
        personalization: PersonalizationSettings | None = None,
    ) -> None:
        self.personal_updates = personal_updates or PersonalUpdateService()
        self.personalization = personalization or PersonalizationSettings()

    def status(self, session: Session, owner_user_id: str) -> OnboardingStatus:
        row = session.execute(
            text("SELECT status FROM onboarding_questionnaire WHERE owner_user_id = :owner"),
            {"owner": owner_user_id},
        ).first()
        if row is None:
            return "not_started"
        return "completed" if str(row[0]) == "completed" else "skipped"

    def skip(self, session: Session, owner_user_id: str) -> None:
        session.execute(
            text(
                """
                INSERT OR IGNORE INTO onboarding_questionnaire (owner_user_id, status)
                VALUES (:owner, 'skipped')
                """
            ),
            {"owner": owner_user_id},
        )

    def complete(
        self,
        session: Session,
        owner_user_id: str,
        *,
        text_answers: dict[str, str],
        excluded_domain_ids: list[str] | None,
        operation_key: str,
    ) -> dict[str, Any]:
        """Remember the explicit answers under the auto-memory rules, propose
        what the system infers from them, and store the fifth answer as the
        primary domains without personalization. Blank answers are left out;
        ``excluded_domain_ids`` of ``None`` leaves that setting unchanged."""
        row = (
            session.execute(
                text(
                    """
                    SELECT status, operation_key, result_json FROM onboarding_questionnaire
                    WHERE owner_user_id = :owner
                    """
                ),
                {"owner": owner_user_id},
            )
            .mappings()
            .first()
        )
        if row is not None and row["status"] == "completed":
            if row["operation_key"] == operation_key:
                return dict(json.loads(str(row["result_json"])))
            raise OnboardingAlreadyCompletedError("the questionnaire was already completed")
        if excluded_domain_ids is not None:
            self.personalization.validate_domain_ids(excluded_domain_ids)

        outcomes: list[dict[str, Any]] = []
        for question_id, answer in text_answers.items():
            target = EXPLICIT_ANSWERS[question_id]
            answer = answer.strip()
            if not answer:
                continue
            outcome = self.personal_updates.apply(
                session,
                PersonalUpdate(
                    memory_type=target.memory_type,
                    state_key=target.state_key,
                    value={"text": answer},
                    source_kind="onboarding_explicit",
                    time_sensitivity=target.time_sensitivity,
                    rationale=f"首次引导回答：{_prompt(question_id)}",
                ),
                operation_key=f"onboarding:{operation_key}:{question_id}",
                owner_user_id=owner_user_id,
            )
            outcomes.append({"question_id": question_id, **outcome})
            inferred = INFERRED_DOMAINS.get(question_id)
            domain_ids = [domain_id for domain_id, _ in match_primary_domains(answer)]
            if inferred is None or not domain_ids:
                continue
            state_key, lead = inferred
            names = "、".join(str(primary_domain_name(domain_id)) for domain_id in domain_ids)
            outcome = self.personal_updates.apply(
                session,
                PersonalUpdate(
                    memory_type="inference",
                    state_key=state_key,
                    value={"text": f"{lead}：{names}", "domain_ids": domain_ids},
                    source_kind="onboarding_inferred",
                    inferred=True,
                    confidence=0.6,
                    rationale=f"由首次引导的回答“{answer}”推断，需要你确认",
                ),
                operation_key=f"onboarding:{operation_key}:{question_id}:inferred",
                owner_user_id=owner_user_id,
            )
            outcomes.append({"question_id": question_id, "inferred": True, **outcome})

        if excluded_domain_ids is not None:
            self.personalization.replace_excluded_domain_ids(session, excluded_domain_ids)

        result = {"answers": outcomes}
        session.execute(
            text(
                """
                INSERT INTO onboarding_questionnaire
                  (owner_user_id, status, operation_key, result_json)
                VALUES (:owner, 'completed', :key, :result)
                ON CONFLICT (owner_user_id) DO UPDATE SET
                  status = 'completed', operation_key = :key, result_json = :result,
                  updated_at = CURRENT_TIMESTAMP
                """
            ),
            {"owner": owner_user_id, "key": operation_key, "result": json_text(result)},
        )
        return result

    def payload(self, session: Session, owner_user_id: str) -> dict[str, Any]:
        status = self.status(session, owner_user_id)
        domains = ordered_primary_domains()
        questions = [
            {**question, "options": domains} if question["kind"] == "domains" else dict(question)
            for question in QUESTIONS
        ]
        return {
            "status": status,
            "should_prompt": status == "not_started",
            "questions": questions,
        }


def _prompt(question_id: str) -> str:
    return next(question["prompt"] for question in QUESTIONS if question["id"] == question_id)
