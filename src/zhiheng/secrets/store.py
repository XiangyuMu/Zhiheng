from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import SecretStr


@dataclass(frozen=True)
class EnvironmentSecretStore:
    allowed_prefix: str = "env:"

    def resolve(self, secret_ref: str) -> SecretStr:
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
