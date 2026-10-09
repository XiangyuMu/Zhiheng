"""Lifecycle readiness requires a submitted parse and preserves timeout evidence."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    'mineru_warmup', Path(__file__).parents[2] / 'scripts/mineru_warmup.py',
)
assert SPEC and SPEC.loader
warmup_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(warmup_module)


def test_warmup_submits_authenticated_probe_before_accepting_readiness(tmp_path, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{}', ''))
    monkeypatch.setattr(warmup_module.subprocess, 'run', run)
    response = io.BytesIO(b'{"status":"ready","model":"ready","task_id":"actual-probe"}')
    monkeypatch.setattr(warmup_module.urllib.request, 'urlopen', lambda *a, **k: response)
    assert warmup_module.warmup(tmp_path / 'compose.yml', tmp_path, 10) == 0
    args = run.call_args.args[0]
    assert args[-2] == '-c'
    assert '/warmup' in args[-1]
    assert 'MINERU_GATEWAY_TOKEN_FILE' in args[-1]
    report = json.loads((tmp_path / 'warmup.json').read_text())
    assert report['status'] == 'ready'
    assert report['readiness']['task_id'] == 'actual-probe'


def test_timeout_preserves_report_when_diagnostic_command_hangs(tmp_path, monkeypatch):
    ticks = iter([0, 0, 0, 2, 2, 2])
    monkeypatch.setattr(warmup_module.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(warmup_module.time, 'sleep', lambda _: None)
    run = Mock(side_effect=[
        subprocess.CompletedProcess([], 1, '', 'sensitive third-party detail'),
        subprocess.TimeoutExpired('logs', 10),
    ])
    monkeypatch.setattr(warmup_module.subprocess, 'run', run)
    assert warmup_module.warmup(tmp_path / 'compose.yml', tmp_path, 1) == 1
    report = json.loads((tmp_path / 'warmup.json').read_text())
    assert report['status'] == 'timeout'
    assert report['warmup_command_exit_code'] == 1
    assert 'sensitive' not in (tmp_path / 'warmup.json').read_text()
    assert (tmp_path / 'warmup-timeout.log').read_text() == 'log_capture_unavailable\n'


def test_unexpected_failure_still_preserves_report(tmp_path, monkeypatch):
    monkeypatch.setattr(warmup_module.subprocess, 'run', Mock(side_effect=[
        RuntimeError('interrupted'), subprocess.CompletedProcess([], 0, 'logs', ''),
    ]))
    with pytest.raises(RuntimeError, match='interrupted'):
        warmup_module.warmup(tmp_path / 'compose.yml', tmp_path, 1)
    report = json.loads((tmp_path / 'warmup.json').read_text())
    assert report['status'] == 'failed'
    assert report['failure_code'] == 'warmup_interrupted'


def test_submission_cannot_extend_readiness_budget(tmp_path, monkeypatch):
    ticks = iter([0, 0, 0, 2, 2])
    monkeypatch.setattr(warmup_module.time, 'monotonic', lambda: next(ticks))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{}', ''))
    monkeypatch.setattr(warmup_module.subprocess, 'run', run)
    probe = Mock()
    monkeypatch.setattr(warmup_module.urllib.request, 'urlopen', probe)
    assert warmup_module.warmup(tmp_path / 'compose.yml', tmp_path, 1) == 1
    probe.assert_not_called()
    assert json.loads((tmp_path / 'warmup.json').read_text())['status'] == 'timeout'
