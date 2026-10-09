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


def warmup(compose_file: Path, diagnostics: Path, timeout: float) -> int:
    diagnostics.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report: dict[str, object] = {
        'started_at': datetime.now(UTC).isoformat(),
        'budget_seconds': timeout,
        'status': 'warming',
    }
    compose = ['docker', 'compose', '-f', str(compose_file), '--profile', 'pdf']
    submitted = False
    ready: dict[str, object] = {}
    try:
        while time.monotonic() - started < timeout:
            remaining = timeout - (time.monotonic() - started)
            try:
                if not submitted:
                    result = subprocess.run(
                        [*compose, 'exec', '-T', 'mineru-gateway', 'python', '-c', WARMUP_REQUEST],
                        capture_output=True, text=True, timeout=min(10, remaining), check=False,
                    )
                    # Never archive raw command stderr: third-party exceptions may
                    # include request details. Preserve a stable diagnostic instead.
                    report['warmup_command_exit_code'] = result.returncode
                    if result.returncode == 0:
                        submitted = True
                if submitted:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    with urllib.request.urlopen(
                        'http://127.0.0.1:9392/ready', timeout=min(5, remaining),
                    ) as response:
                        ready = json.loads(response.read())
                    report['readiness'] = ready
                    if ready.get('status') == 'ready' and ready.get('model') == 'ready':
                        report['status'] = 'ready'
                        return 0
            except urllib.error.HTTPError as exc:
                report['readiness_http_status'] = exc.code
                try:
                    report['readiness'] = json.loads(exc.read())
                except ValueError:
                    report['readiness_error'] = 'invalid_json'
            except (OSError, ValueError, subprocess.TimeoutExpired):
                report['readiness_error'] = 'probe_unavailable'
            time.sleep(max(0, min(2, timeout - (time.monotonic() - started))))
        report['status'] = 'timeout'
        return 1
    except BaseException:
        report['status'] = 'failed'
        report['failure_code'] = 'warmup_interrupted'
        raise
    finally:
        report['finished_at'] = datetime.now(UTC).isoformat()
        report['elapsed_seconds'] = time.monotonic() - started
        (diagnostics / 'warmup.json').write_text(json.dumps(report, indent=2) + '\n')
        if report['status'] != 'ready':
            # Bounded log capture cannot prevent the failure report being saved.
            try:
                logs = subprocess.run(
                    [*compose, 'logs', '--tail=200', 'mineru-worker', 'mineru-gateway'],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                (diagnostics / 'warmup-timeout.log').write_text(logs.stdout)
            except (OSError, subprocess.TimeoutExpired):
                (diagnostics / 'warmup-timeout.log').write_text('log_capture_unavailable\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-file', type=Path, required=True)
    parser.add_argument('--diagnostics', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=1800)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    return warmup(args.compose_file, args.diagnostics, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
