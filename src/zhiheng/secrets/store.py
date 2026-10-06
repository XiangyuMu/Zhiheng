from __future__ import annotations

import base64
import json
import os
import secrets as pysecrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zhiheng.core.ids import new_id, sha256_text

LOCAL_SECRET_PREFIX = "local:"
PROVIDER_SECRET_ALGORITHM = "AES-256-GCM"
_MASTER_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretUnavailable(PermissionError):
    """Raised when a configured provider secret cannot be resolved safely."""


class MasterKeyUnavailable(SecretUnavailable):
    """Raised when the native keyring cannot provide the instance master key."""


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str, *, provider_id: str | None = None) -> SecretStr: ...


class MasterKeyBackend(Protocol):
    def get_master_key(self, instance_id: str) -> bytes: ...

    def get_or_create_master_key(self, instance_id: str) -> bytes: ...


@dataclass(frozen=True)
class SecretStatus:
    configured: bool
    status: str
    fingerprint: str | None = None


@dataclass(frozen=True)
class StoredProviderSecret:
    secret_ref: str
    fingerprint: str
    version: int


@dataclass
class EnvironmentSecretStore:
    allowed_prefix: str = "env:"

    def resolve(self, secret_ref: str, *, provider_id: str | None = None) -> SecretStr:
        del provider_id
        if not secret_ref.startswith(self.allowed_prefix):
            raise ValueError("secret_ref must use an approved secret reference scheme")
        env_name = secret_ref.removeprefix(self.allowed_prefix)
        if not env_name.startswith("ZHIHENG_PRIVATE_"):
            raise ValueError("secret_ref does not point to a private secret")
        value = os.environ.get(env_name)
        if value is None:
            raise KeyError("secret reference is not configured")
        if value == "":
            raise ValueError("secret reference is empty")
        return SecretStr(value)


class InMemoryMasterKeyBackend:
    def __init__(self, key: bytes | None = None, *, available: bool = True) -> None:
        self._keys: dict[str, bytes] = {}
        self._initial_key = key
        self.available = available

    def get_master_key(self, instance_id: str) -> bytes:
        if not self.available:
            raise MasterKeyUnavailable("native keyring backend is unavailable")
        try:
            return self._keys[instance_id]
        except KeyError as exc:
            raise MasterKeyUnavailable("provider master key is unavailable") from exc

    def get_or_create_master_key(self, instance_id: str) -> bytes:
        if not self.available:
            raise MasterKeyUnavailable("native keyring backend is unavailable")
        existing = self._keys.get(instance_id)
        if existing is not None:
            return existing
        key = self._initial_key or AESGCM.generate_key(bit_length=256)
        if len(key) != _MASTER_KEY_BYTES:
            raise MasterKeyUnavailable("provider master key has invalid length")
        self._keys[instance_id] = key
        return key


class NativeKeyringMasterKeyBackend:
    service_name = "zhiheng.provider-secrets"

    def __init__(self) -> None:
        self._keyring: Any | None = None

    def get_master_key(self, instance_id: str) -> bytes:
        try:
            value = self._platform_keyring().get_password(self.service_name, instance_id)
            if value is None:
                raise MasterKeyUnavailable("provider master key is unavailable")
            return _decode_master_key(value)
        except MasterKeyUnavailable:
            raise
        except Exception as exc:
            raise MasterKeyUnavailable("native keyring backend is unavailable") from exc

    def get_or_create_master_key(self, instance_id: str) -> bytes:
        try:
            keyring_backend = self._platform_keyring()
            value = keyring_backend.get_password(self.service_name, instance_id)
            if value is not None:
                return _decode_master_key(value)
            key = AESGCM.generate_key(bit_length=256)
            keyring_backend.set_password(self.service_name, instance_id, _b64encode(key))
            return key
        except MasterKeyUnavailable:
            raise
        except Exception as exc:
            raise MasterKeyUnavailable("native keyring backend is unavailable") from exc

    def _platform_keyring(self) -> Any:
        if self._keyring is None:
            self._keyring = self._load_platform_keyring()
        return self._keyring

    @staticmethod
    def _load_platform_keyring() -> Any:
        platform_name = str(sys.platform)
        try:
            if platform_name == "darwin":
                module = import_module("keyring.backends.macOS")
                return module.Keyring()
            if platform_name.startswith("linux"):
                module = import_module("keyring.backends.SecretService")
                return module.Keyring()
        except Exception as exc:  # pragma: no cover - exercised by controlled adapters.
            raise MasterKeyUnavailable("native keyring backend is unavailable") from exc
        raise MasterKeyUnavailable("native keyring backend is unsupported on this platform")


class ProviderSecretStore:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session] | None = None,
        master_key_backend: MasterKeyBackend | None = None,
        instance_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._master_key_backend = master_key_backend or NativeKeyringMasterKeyBackend()
        self._instance_id = instance_id
        self._environment = EnvironmentSecretStore()

    def bind_session_factory(self, session_factory: sessionmaker[Session]) -> ProviderSecretStore:
        if self._session_factory is session_factory:
            return self
        return ProviderSecretStore(
            session_factory=session_factory,
            master_key_backend=self._master_key_backend,
            instance_id=self._instance_id,
        )

    def store(
        self,
        session: Session,
        *,
        provider_id: str,
        secret: SecretStr,
    ) -> StoredProviderSecret:
        plaintext = secret.get_secret_value()
        if not plaintext:
            raise ValueError("provider key must not be empty")
        instance_id = self._instance_id or _provider_secret_instance_id(session, create=True)
        master_key = self._master_key_backend.get_or_create_master_key(instance_id)
        secret_id = new_id()
        version = 1
        nonce = pysecrets.token_bytes(_NONCE_BYTES)
        aad = self._associated_data(
            secret_id=secret_id,
            provider_id=provider_id,
            version=version,
            algorithm=PROVIDER_SECRET_ALGORITHM,
            instance_id=instance_id,
        )
        ciphertext = AESGCM(master_key).encrypt(nonce, plaintext.encode("utf-8"), aad)
        fingerprint = sha256_text(plaintext)[:12]
        session.execute(
            text(
                """
                INSERT INTO provider_secret_records (
                  id, provider_id, secret_version, algorithm, nonce_b64,
                  ciphertext_b64, aad_json, secret_fingerprint, status
                ) VALUES (
                  :id, :provider_id, :secret_version, :algorithm, :nonce_b64,
                  :ciphertext_b64, :aad_json, :secret_fingerprint, 'active'
                )
                """
            ),
            {
                "id": secret_id,
                "provider_id": provider_id,
                "secret_version": version,
                "algorithm": PROVIDER_SECRET_ALGORITHM,
                "nonce_b64": _b64encode(nonce),
                "ciphertext_b64": _b64encode(ciphertext),
                "aad_json": aad.decode("utf-8"),
                "secret_fingerprint": fingerprint,
            },
        )
        return StoredProviderSecret(
            secret_ref=f"{LOCAL_SECRET_PREFIX}{secret_id}",
            fingerprint=fingerprint,
            version=version,
        )

    def resolve(self, secret_ref: str, *, provider_id: str | None = None) -> SecretStr:
        if secret_ref.startswith("env:"):
            return self._environment.resolve(secret_ref, provider_id=provider_id)
        if not secret_ref.startswith(LOCAL_SECRET_PREFIX):
            raise ValueError("secret_ref must use an approved secret reference scheme")
        if provider_id is None:
            raise SecretUnavailable("provider secret binding is required")
        if self._session_factory is None:
            raise SecretUnavailable("provider secret database is unavailable")
        secret_id = secret_ref.removeprefix(LOCAL_SECRET_PREFIX)
        with self._session_factory() as session:
            row = _provider_secret_row(session, secret_id)
            instance_id = self._instance_id or _provider_secret_instance_id(session, create=False)
        if row is None or str(row["status"]) != "active":
            raise SecretUnavailable("provider secret is unavailable")
        return SecretStr(self._decrypt_row(row, provider_id=provider_id, instance_id=instance_id))

    def status(
        self,
        session: Session,
        *,
        secret_ref: str | None,
        provider_id: str,
    ) -> SecretStatus:
        if not secret_ref:
            return SecretStatus(configured=False, status="missing")
        if secret_ref.startswith("env:"):
            return SecretStatus(
                configured=True,
                status="configured",
                fingerprint=sha256_text(secret_ref)[:12],
            )
        if not secret_ref.startswith(LOCAL_SECRET_PREFIX):
            return SecretStatus(configured=True, status="unavailable")
        secret_id = secret_ref.removeprefix(LOCAL_SECRET_PREFIX)
        row = _provider_secret_row(session, secret_id)
        if row is None or str(row["status"]) != "active":
            return SecretStatus(configured=True, status="unavailable")
        fingerprint = str(row["secret_fingerprint"]) if row["secret_fingerprint"] else None
        try:
            instance_id = self._instance_id or _provider_secret_instance_id(session, create=False)
            self._decrypt_row(row, provider_id=provider_id, instance_id=instance_id)
        except SecretUnavailable:
            return SecretStatus(configured=True, status="unavailable", fingerprint=fingerprint)
        return SecretStatus(configured=True, status="configured", fingerprint=fingerprint)

    def _decrypt_row(self, row: Mapping[str, Any], *, provider_id: str, instance_id: str) -> str:
        owner_provider_id = str(row["provider_id"])
        if owner_provider_id != provider_id:
            raise SecretUnavailable("provider secret binding mismatch")
        algorithm = str(row["algorithm"])
        version = int(row["secret_version"])
        secret_id = str(row["id"])
        if algorithm != PROVIDER_SECRET_ALGORITHM:
            raise SecretUnavailable("provider secret algorithm is unsupported")
        try:
            nonce = base64.b64decode(str(row["nonce_b64"]), validate=True)
            ciphertext = base64.b64decode(str(row["ciphertext_b64"]), validate=True)
            aad = str(row["aad_json"]).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise SecretUnavailable("provider secret metadata is invalid") from exc
        expected_aad = self._associated_data(
            secret_id=secret_id,
            provider_id=provider_id,
            version=version,
            algorithm=algorithm,
            instance_id=instance_id,
        )
        if aad != expected_aad:
            raise SecretUnavailable("provider secret authenticated data mismatch")
        try:
            master_key = self._master_key_backend.get_master_key(instance_id)
            plaintext = AESGCM(master_key).decrypt(nonce, ciphertext, aad)
        except (InvalidTag, ValueError, MasterKeyUnavailable) as exc:
            raise SecretUnavailable("provider secret cannot be decrypted") from exc
        return plaintext.decode("utf-8")

    def _associated_data(
        self,
        *,
        secret_id: str,
        provider_id: str,
        version: int,
        algorithm: str,
        instance_id: str,
    ) -> bytes:
        return json.dumps(
            {
                "algorithm": algorithm,
                "instance_id": instance_id,
                "provider_id": provider_id,
                "secret_id": secret_id,
                "version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def _provider_secret_row(session: Session, secret_id: str) -> Mapping[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT id, provider_id, secret_version, algorithm, nonce_b64,
                       ciphertext_b64, aad_json, secret_fingerprint, status
                FROM provider_secret_records
                WHERE id = :secret_id
                """
            ),
            {"secret_id": secret_id},
        )
        .mappings()
        .one_or_none()
    )
    return cast(Mapping[str, Any] | None, row)


def _provider_secret_instance_id(session: Session, *, create: bool) -> str:
    row = session.execute(
        text(
            """
            SELECT instance_id
            FROM provider_secret_instances
            WHERE singleton_id = 'default'
            """
        )
    ).one_or_none()
    if row is not None:
        return str(row[0])
    if not create:
        raise MasterKeyUnavailable("provider secret instance is unavailable")
    instance_id = pysecrets.token_hex(16)
    session.execute(
        text(
            """
            INSERT INTO provider_secret_instances (singleton_id, instance_id)
            VALUES ('default', :instance_id)
            """
        ),
        {"instance_id": instance_id},
    )
    return instance_id


def _decode_master_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise MasterKeyUnavailable("provider master key has invalid encoding") from exc
    if len(key) != _MASTER_KEY_BYTES:
        raise MasterKeyUnavailable("provider master key has invalid length")
    return key


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
