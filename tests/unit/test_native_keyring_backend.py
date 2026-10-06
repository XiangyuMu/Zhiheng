from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    keyring = _FakeKeyring()
    _FakeKeyring.values = {}
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", platform)
    monkeypatch.setattr("zhiheng.secrets.store.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )
    backend = NativeKeyringMasterKeyBackend()
    first = backend.get_or_create_master_key("instance-a")
    assert len(first) == 32
    assert backend.get_master_key("instance-a") == first
    assert NativeKeyringMasterKeyBackend().get_master_key("instance-a") == first


@pytest.mark.parametrize(
    ("platform", "expected_module"),
    [("darwin", "keyring.backends.macOS"), ("linux", "keyring.backends.SecretService")],
)
def test_native_keyring_selects_only_the_platform_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, expected_module: str
) -> None:
    imported: list[str] = []
    keyring = _FakeKeyring()
    _FakeKeyring.values = {}
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", platform)
    monkeypatch.setattr("zhiheng.secrets.store.Path.home", classmethod(lambda cls: tmp_path))

    def load(name: str) -> SimpleNamespace:
        imported.append(name)
        return SimpleNamespace(Keyring=lambda: keyring)

    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        load,
    )

    NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")

    assert imported == [expected_module]


def test_native_keyring_rejects_unsupported_platform_without_creating_a_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "win32")
    monkeypatch.setattr("zhiheng.secrets.store.Path.home", classmethod(lambda cls: tmp_path))

    with pytest.raises(MasterKeyUnavailable, match="unsupported"):
        NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")

    assert not list(tmp_path.rglob("*"))


def test_native_keyring_rejects_symlinked_lock_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "linux")
    monkeypatch.setattr("zhiheng.secrets.store.Path.home", classmethod(lambda cls: tmp_path))
    target = tmp_path / "target"
    target.mkdir()
    lock_parent = tmp_path / ".local" / "state" / "zhiheng"
    lock_parent.parent.mkdir(parents=True)
    lock_parent.symlink_to(target, target_is_directory=True)
    keyring = _FakeKeyring()
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )

    with pytest.raises(MasterKeyUnavailable, match="lock directory is insecure"):
        NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_keyring_allows_private_lock_below_standard_user_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", platform)
    monkeypatch.setattr("zhiheng.secrets.store.Path.home", classmethod(lambda cls: tmp_path))
    parent = (
        tmp_path / ".local" / "state"
        if platform == "linux"
        else tmp_path / "Library" / "Application Support"
    )
    parent.mkdir(parents=True)
    for directory in [tmp_path, *parent.parents]:
        if directory.is_relative_to(tmp_path):
            directory.chmod(0o755)
    keyring = _FakeKeyring()
    _FakeKeyring.values = {}
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )

    key = NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")

    assert len(key) == 32
    lock_dir = (
        tmp_path / ".local" / "state" / "zhiheng" / "locks"
        if platform == "linux"
        else tmp_path / "Library" / "Application Support" / "Zhiheng" / "locks"
    )
    assert (lock_dir.stat().st_mode & 0o077) == 0


def test_native_keyring_rejects_group_writable_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "linux")
    monkeypatch.setattr("zhiheng.secrets.store.Path.home", classmethod(lambda cls: tmp_path))
    parent = tmp_path / ".local"
    parent.mkdir()
    parent.chmod(0o775)
    keyring = _FakeKeyring()
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )

    with pytest.raises(MasterKeyUnavailable, match="lock directory is insecure"):
        NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")


def test_native_keyring_rejects_foreign_lock_directory_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "linux")
    monkeypatch.setattr("zhiheng.secrets.store.Path.home", classmethod(lambda cls: tmp_path))
    keyring = _FakeKeyring()
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )
    current_uid = os.getuid()
    monkeypatch.setattr("zhiheng.secrets.store.os.getuid", lambda: current_uid + 1)

    with pytest.raises(MasterKeyUnavailable, match="lock directory is insecure"):
        NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")


@pytest.mark.skipif(os.name != "posix", reason="native lock uses POSIX file locking")
def test_native_keyring_first_creation_is_shared_across_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keyring_path = tmp_path / "fake-keyring.json"
    script = textwrap.dedent(
        """
        import json
        import os
        import time
        from pathlib import Path
        from types import SimpleNamespace
        import zhiheng.secrets.store as store

        class Keyring:
            def __init__(self):
                self.path = Path(os.environ["ZHIHENG_FAKE_KEYRING"])
            def get_password(self, service, username):
                if not self.path.exists():
                    return None
                return json.loads(self.path.read_text()).get(f"{service}:{username}")
            def set_password(self, service, username, value):
                self.path.write_text(json.dumps({f"{service}:{username}": value}))

        store.sys.platform = "linux"
        store.import_module = lambda name: SimpleNamespace(Keyring=Keyring)
        ready = Path(os.environ["ZHIHENG_READY"])
        ready.touch()
        deadline = time.monotonic() + 10
        while len(list(ready.parent.glob("ready-*"))) < 2:
            if time.monotonic() > deadline:
                raise TimeoutError("peer did not start")
            time.sleep(0.01)
        key = store.NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-race")
        Path(os.environ["ZHIHENG_RESULT"]).write_bytes(key)
        """
    )
    env_base = os.environ.copy()
    env_base.update(
        {
            "HOME": str(tmp_path),
            "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
            "ZHIHENG_FAKE_KEYRING": str(keyring_path),
        }
    )
    processes: list[subprocess.Popen[bytes]] = []
    for index in range(2):
        env = env_base | {
            "ZHIHENG_READY": str(tmp_path / f"ready-{index}"),
            "ZHIHENG_RESULT": str(tmp_path / f"result-{index}"),
        }
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", script],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    outputs = [process.communicate(timeout=15) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    assert (tmp_path / "result-0").read_bytes() == (tmp_path / "result-1").read_bytes()
    assert len(json.loads(keyring_path.read_text())) == 1


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


def test_native_keyring_rejects_invalid_persisted_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = _FakeKeyring()
    _FakeKeyring.values = {
        (NativeKeyringMasterKeyBackend.service_name, "instance-a"): "not-base64"
    }
    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "darwin")
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=lambda: keyring),
    )

    with pytest.raises(MasterKeyUnavailable, match="invalid encoding"):
        NativeKeyringMasterKeyBackend().get_master_key("instance-a")


@pytest.mark.parametrize("operation", ["get", "set"])
def test_native_keyring_normalizes_backend_failures(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    class _FailingKeyring:
        def get_password(self, service: str, username: str) -> str | None:
            if operation == "get":
                raise RuntimeError("backend down")
            return None

        def set_password(self, service: str, username: str, value: str) -> None:
            if operation == "set":
                raise RuntimeError("backend down")

    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "darwin")
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=_FailingKeyring),
    )

    with pytest.raises(MasterKeyUnavailable, match="unavailable"):
        if operation == "get":
            NativeKeyringMasterKeyBackend().get_master_key("instance-a")
        else:
            NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")


def test_native_keyring_rejects_readback_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NonPersistingKeyring:
        def get_password(self, service: str, username: str) -> str | None:
            return None

        def set_password(self, service: str, username: str, value: str) -> None:
            del service, username, value

    monkeypatch.setattr("zhiheng.secrets.store.sys.platform", "darwin")
    monkeypatch.setattr(
        "zhiheng.secrets.store.import_module",
        lambda name: SimpleNamespace(Keyring=_NonPersistingKeyring),
    )

    with pytest.raises(MasterKeyUnavailable, match="not persisted"):
        NativeKeyringMasterKeyBackend().get_or_create_master_key("instance-a")
