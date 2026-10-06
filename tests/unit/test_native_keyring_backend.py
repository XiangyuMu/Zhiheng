from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from zhiheng.secrets.store import MasterKeyUnavailable, NativeKeyringMasterKeyBackend


class _FakeKeyring:
    values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, value: str) -> None:
        self.values[(service, username)] = value


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_keyring_selects_platform_adapter_and_reuses_persisted_key(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    keyring = _FakeKeyring()
    _FakeKeyring.values = {}
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", platform)
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )
    backend = NativeKeyringMasterKeyBackend()
    first = backend.get_or_create_master_key("instance-a")
    assert len(first) == 32
    assert backend.get_master_key("instance-a") == first
    assert NativeKeyringMasterKeyBackend().get_master_key("instance-a") == first


def test_native_keyring_rejects_invalid_persisted_key(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = _FakeKeyring()
    _FakeKeyring.values = {
        (NativeKeyringMasterKeyBackend.service_name, "instance-a"): base64.b64encode(
            b"short"
        ).decode()
    }
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "darwin")
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )
    with pytest.raises(MasterKeyUnavailable, match="invalid length"):
        NativeKeyringMasterKeyBackend().get_master_key("instance-a")
