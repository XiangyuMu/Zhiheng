"""Bounded real-parser warmup, with durable success and failure diagnostics."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# Execute inside the gateway so the service secret never enters host arguments.
WARMUP_REQUEST = """
import os, pathlib, urllib.request
secret = pathlib.Path(os.environ['MINERU_GATEWAY_TOKEN_FILE']).read_text().strip()
request = urllib.request.Request('http://127.0.0.1:9392/warmup', method='POST',
    headers={'Authorization': 'Bearer ' + secret})
with urllib.request.urlopen(request, timeout=5) as response:
    print(response.read().decode())
"""

DIAGNOSTIC_SERVICES = ("mineru-gateway", "mineru-worker")

MODEL_CACHE_REQUEST = """
import hashlib, json
from pathlib import Path

root = Path('/opt/mineru-models')
config = root / 'mineru.json'

def entry_summary(path):
    if path.is_dir():
        return {'name': path.name, 'type': 'directory'}
    stat = path.stat()
    return {'name': path.name, 'type': 'file', 'size_bytes': stat.st_size}

summary = {
    'root': str(root),
    'root_exists': root.exists(),
    'config_path': str(config),
    'config_exists': config.exists(),
}
if root.exists():
    summary['top_level_entries'] = [
        entry_summary(path) for path in sorted(root.iterdir(), key=lambda item: item.name)[:50]
    ]
if config.exists():
    payload = config.read_bytes()
    summary['config_size_bytes'] = len(payload)
    summary['config_sha256'] = hashlib.sha256(payload).hexdigest()
print(json.dumps(summary, sort_keys=True))
"""


def _safe_json_loads(value: str) -> object:
    stripped = value.strip()
    if not stripped:
        return []
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        values = []
        for line in stripped.splitlines():
            if line.strip():
                values.append(json.loads(line))
        return values


def _capture_json_command(
    command: list[str], *, timeout: float = 10
) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "error": "timeout"}
    except OSError:
        return {"status": "unavailable", "error": "command_unavailable"}
    if result.returncode != 0:
        return {"status": "unavailable", "exit_code": result.returncode}
    try:
        return {"status": "captured", "data": _safe_json_loads(result.stdout)}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"status": "unavailable", "error": "invalid_json"}


def _capture_text_command(
    command: list[str], *, timeout: float = 10
) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "error": "timeout"}
    except OSError:
        return {"status": "unavailable", "error": "command_unavailable"}
    if result.returncode != 0:
        return {"status": "unavailable", "exit_code": result.returncode}
    return {"status": "captured", "data": result.stdout}


def _container_ids(compose: list[str]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for service in DIAGNOSTIC_SERVICES:
        result = _capture_text_command([*compose, "ps", "-q", service], timeout=5)
        if result["status"] == "captured":
            container_id = str(result["data"]).strip().splitlines()
            if container_id:
                ids[service] = container_id[0]
    return ids


def _summarize_inspect(data: object) -> list[dict[str, object]]:
    if not isinstance(data, list):
        return []
    summaries: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        host_config = item.get("HostConfig")
        config = item.get("Config")
        state = item.get("State")
        network_settings = item.get("NetworkSettings")
        summaries.append(
            {
                "id": item.get("Id"),
                "name": item.get("Name"),
                "image": item.get("Image"),
                "state": state if isinstance(state, dict) else {},
                "resource_limits": {
                    "memory": host_config.get("Memory"),
                    "nano_cpus": host_config.get("NanoCpus"),
                    "pids_limit": host_config.get("PidsLimit"),
                    "readonly_rootfs": host_config.get("ReadonlyRootfs"),
                    "cap_drop": host_config.get("CapDrop"),
                    "security_opt": host_config.get("SecurityOpt"),
                    "tmpfs": host_config.get("Tmpfs"),
                }
                if isinstance(host_config, dict)
                else {},
                "image_ref": config.get("Image") if isinstance(config, dict) else None,
                "networks": sorted(network_settings.get("Networks", {}).keys())
                if isinstance(network_settings, dict)
                and isinstance(network_settings.get("Networks"), dict)
                else [],
            }
        )
    return summaries


def _collect_diagnostics(compose: list[str]) -> dict[str, object]:
    # Unit tests and callers that only exercise the lifecycle state machine may
    # provide a placeholder compose path.  There is no useful Docker evidence
    # to collect in that case, and attempting commands would obscure the
    # original warmup result.  Real deployments always pass an existing file.
    compose_file = Path(compose[2]) if len(compose) > 2 else None
    if compose_file is None or not compose_file.is_file():
        return {"status": "unavailable", "error": "compose_file_unavailable"}
    diagnostics: dict[str, object] = {
        "docker_compose_images": _capture_json_command([*compose, "images", "--format", "json"]),
        "docker_compose_ps": _capture_json_command([*compose, "ps", "--format", "json"]),
    }
    container_ids = _container_ids(compose)
    diagnostics["container_ids"] = container_ids
    if container_ids:
        inspect = _capture_json_command(["docker", "inspect", *container_ids.values()])
        if inspect["status"] == "captured":
            inspect["data"] = _summarize_inspect(inspect["data"])
        diagnostics["docker_inspect"] = inspect
        diagnostics["docker_stats"] = _capture_json_command(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *container_ids.values()]
        )
    else:
        diagnostics["docker_inspect"] = {"status": "unavailable", "error": "no_containers"}
        diagnostics["docker_stats"] = {"status": "unavailable", "error": "no_containers"}
    diagnostics["mineru_model_cache"] = _capture_json_command(
        [*compose, "exec", "-T", "mineru-worker", "python", "-c", MODEL_CACHE_REQUEST]
    )
    return diagnostics


def warmup(compose_file: Path, diagnostics: Path, timeout: float) -> int:
    diagnostics.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report: dict[str, object] = {
        "started_at": datetime.now(UTC).isoformat(),
        "budget_seconds": timeout,
        "status": "warming",
    }
    compose = ["docker", "compose", "-f", str(compose_file), "--profile", "pdf"]
    submitted = False
    ready: dict[str, object] = {}
    try:
        while time.monotonic() - started < timeout:
            remaining = timeout - (time.monotonic() - started)
            try:
                if not submitted:
                    result = subprocess.run(
                        [*compose, "exec", "-T", "mineru-gateway", "python", "-c", WARMUP_REQUEST],
                        capture_output=True,
                        text=True,
                        timeout=min(10, remaining),
                        check=False,
                    )
                    # Never archive raw command stderr: third-party exceptions may
                    # include request details. Preserve a stable diagnostic instead.
                    report["warmup_command_exit_code"] = result.returncode
                    if result.returncode == 0:
                        submitted = True
                if submitted:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    with urllib.request.urlopen(
                        "http://127.0.0.1:9392/ready",
                        timeout=min(5, remaining),
                    ) as response:
                        ready = json.loads(response.read())
                    report["readiness"] = ready
                    if ready.get("status") == "ready" and ready.get("model") == "ready":
                        report["status"] = "ready"
                        return 0
            except urllib.error.HTTPError as exc:
                report["readiness_http_status"] = exc.code
                try:
                    report["readiness"] = json.loads(exc.read())
                except ValueError:
                    report["readiness_error"] = "invalid_json"
            except (OSError, ValueError, subprocess.TimeoutExpired):
                report["readiness_error"] = "probe_unavailable"
            time.sleep(max(0, min(2, timeout - (time.monotonic() - started))))
        report["status"] = "timeout"
        return 1
    except BaseException:
        report["status"] = "failed"
        report["failure_code"] = "warmup_interrupted"
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["elapsed_seconds"] = time.monotonic() - started
        try:
            report["diagnostics"] = _collect_diagnostics(compose)
        except BaseException:
            report["diagnostics"] = {"status": "unavailable", "error": "diagnostic_failure"}
        (diagnostics / "warmup.json").write_text(json.dumps(report, indent=2) + "\n")
        if report["status"] != "ready":
            # Bounded log capture cannot prevent the failure report being saved.
            try:
                logs = subprocess.run(
                    [*compose, "logs", "--tail=200", "mineru-worker", "mineru-gateway"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                (diagnostics / "warmup-timeout.log").write_text(logs.stdout)
            except (OSError, subprocess.TimeoutExpired):
                (diagnostics / "warmup-timeout.log").write_text("log_capture_unavailable\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return warmup(args.compose_file, args.diagnostics, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
