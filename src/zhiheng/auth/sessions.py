from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.orm import Session

from zhiheng.core.ids import new_id, sha256_text


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    user_id: str
    token: str
    expires_at: datetime


class SessionService:
    def __init__(self, password_hasher: PasswordHasher | None = None) -> None:
        self._password_hasher = password_hasher or PasswordHasher()

    def bootstrap_single_user(self, session: Session, *, username: str, password: str) -> str:
        existing_user_id = session.execute(text("SELECT id FROM auth_users LIMIT 1")).scalar()
        if existing_user_id is not None:
            return str(existing_user_id)
        if not username:
            raise ValueError("username cannot be empty")
        if len(password) < 12:
            raise ValueError("password must be at least 12 characters")

        user_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO auth_users (id, username, password_hash, status)
                VALUES (:id, :username, :password_hash, 'active')
                """
            ),
            {
                "id": user_id,
                "username": username,
                "password_hash": self._password_hasher.hash(password),
            },
        )
        return user_id

    def authenticate(
        self,
        session: Session,
        *,
        username: str,
        password: str,
        ttl: timedelta = timedelta(hours=12),
    ) -> AuthenticatedSession:
        row = (
            session.execute(
                text(
                    """
                SELECT id, password_hash
                FROM auth_users
                WHERE username = :username
                  AND status = 'active'
                """
                ),
                {"username": username},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PermissionError("invalid credentials")

        try:
            verified = self._password_hasher.verify(str(row["password_hash"]), password)
        except VerifyMismatchError as exc:
            raise PermissionError("invalid credentials") from exc
        if not verified:
            raise PermissionError("invalid credentials")

        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(UTC) + ttl
        session_id = new_id()
        session.execute(
            text(
                """
                INSERT INTO auth_sessions (
                  id, user_id, token_hash, status, expires_at
                )
                VALUES (
                  :id, :user_id, :token_hash, 'active', :expires_at
                )
                """
            ),
            {
                "id": session_id,
                "user_id": row["id"],
                "token_hash": sha256_text(token),
                "expires_at": expires_at,
            },
        )
        return AuthenticatedSession(
            session_id=session_id,
            user_id=str(row["id"]),
            token=token,
            expires_at=expires_at,
        )

    def resolve_session(self, session: Session, token: str) -> str:
        row = (
            session.execute(
                text(
                    """
                SELECT au.id AS user_id
                FROM auth_sessions auth
                JOIN auth_users au ON au.id = auth.user_id
                WHERE auth.token_hash = :token_hash
                  AND auth.status = 'active'
                  AND auth.expires_at > :now
                  AND au.status = 'active'
                """
                ),
                {"token_hash": sha256_text(token), "now": datetime.now(UTC)},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PermissionError("invalid or expired session")
        session.execute(
            text(
                """
                UPDATE auth_sessions
                SET last_seen_at = CURRENT_TIMESTAMP
                WHERE token_hash = :token_hash
                """
            ),
            {"token_hash": sha256_text(token)},
        )
        return str(row["user_id"])

    def refresh(
        self, session: Session, token: str, *, ttl: timedelta = timedelta(hours=12)
    ) -> AuthenticatedSession:
        row = (
            session.execute(
                text("""
            SELECT id, user_id FROM auth_sessions
            WHERE token_hash = :token_hash AND status = 'active' AND expires_at > :now
        """),
                {"token_hash": sha256_text(token), "now": datetime.now(UTC)},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PermissionError("invalid or expired session")
        new_token = secrets.token_urlsafe(48)
        expires_at = datetime.now(UTC) + ttl
        new_id_value = new_id()
        session.execute(
            text("UPDATE auth_sessions SET status = 'revoked' WHERE id = :id"), {"id": row["id"]}
        )
        session.execute(
            text("""
            INSERT INTO auth_sessions (id, user_id, token_hash, status, expires_at)
            VALUES (:id, :user_id, :token_hash, 'active', :expires_at)
        """),
            {
                "id": new_id_value,
                "user_id": row["user_id"],
                "token_hash": sha256_text(new_token),
                "expires_at": expires_at,
            },
        )
        return AuthenticatedSession(new_id_value, str(row["user_id"]), new_token, expires_at)

    def revoke_session(self, session: Session, token: str) -> None:
        session.execute(
            text(
                """
                UPDATE auth_sessions
                SET status = 'revoked'
                WHERE token_hash = :token_hash
                """
            ),
            {"token_hash": sha256_text(token)},
        )
