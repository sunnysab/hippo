"""Rehearse deploy.sh offline, with every remote command faked onto PATH.

deploy.sh drives production over ssh/rsync, so these tests cover the workflow
only: which command runs in which order, and what a failure does. No host is
contacted, and passing this file is not a substitute for watching a real deploy.

deploy.sh is a local, gitignored tool, so the module skips when it is absent.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = REPO_ROOT / 'deploy.sh'

REQUIRED_FILES = (
    'hippo/knowledge/worker.py',
    'hippo/repositories/knowledge.py',
    'hippo/api/routers/knowledge.py',
    'hippo-knowledge-worker.service',
    'frontend/src/pages/knowledge/KnowledgePage.tsx',
)

FAILING_SSH = """#!/bin/bash
stdin="$(cat)"
printf 'ssh %s\\n%s\\n' "$*" "$stdin" >> "$FAKE_LOG"
if [ -n "${FAKE_FAIL_ON:-}" ] && [[ "$* $stdin" == *"$FAKE_FAIL_ON"* ]]; then
    exit 1
fi
exit 0
"""

RECORDING = """#!/bin/bash
printf '{name} %s\\n' "$*" >> "$FAKE_LOG"
exit 0
"""

NODE = """#!/bin/bash
printf 'node %s\\n' "$*" >> "$FAKE_LOG"
echo v24.0.0
"""

pytestmark = pytest.mark.skipif(
    not DEPLOY_SH.exists(),
    reason='deploy.sh is a local, gitignored tool; without it there is nothing to rehearse',
)


def _fake_bin(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH prefix holding recording stand-ins for every external command."""
    directory = tmp_path / 'fake-bin'
    directory.mkdir()
    log = tmp_path / 'commands.log'
    scripts = {
        'ssh': FAILING_SSH,
        'rsync': RECORDING.format(name='rsync'),
        'npm': RECORDING.format(name='npm'),
        'node': NODE,
    }
    for name, body in scripts.items():
        path = directory / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory, log


def _tree(tmp_path: Path, *, dist: bool = True) -> Path:
    """The smallest checkout deploy.sh accepts, so nothing outside tmp_path is read."""
    tree = tmp_path / 'tree'
    for relative in REQUIRED_FILES:
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('placeholder\n')
    if dist:
        (tree / 'frontend/dist/assets').mkdir(parents=True)
        (tree / 'frontend/dist/index.html').write_text('<!doctype html>\n')
        (tree / 'frontend/dist/assets/app-abc123.js').write_text('export {};\n')
    return tree


def _run(tree: Path, fake_bin: Path, log: Path, *, fail_on: str = '', skip_build: bool = False):
    env = dict(os.environ)
    env['PATH'] = f'{fake_bin}{os.pathsep}{env["PATH"]}'
    env['FAKE_LOG'] = str(log)
    env.pop('FAKE_FAIL_ON', None)
    env.pop('SKIP_FRONTEND_BUILD', None)
    if fail_on:
        env['FAKE_FAIL_ON'] = fail_on
    if skip_build:
        env['SKIP_FRONTEND_BUILD'] = '1'
    return subprocess.run(
        ['bash', str(DEPLOY_SH)],
        cwd=tree,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _log(log: Path) -> str:
    return log.read_text() if log.exists() else ''


def test_build_runs_and_services_start_after_migration(tmp_path):
    tree = _tree(tmp_path)
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log)

    assert result.returncode == 0, result.stderr
    recorded = _log(log)
    assert 'npm install --no-audit --no-fund' in recorded
    assert 'npm run build' in recorded
    assert recorded.index('init-db') < recorded.index('systemctl start hippo.service')


def test_incomplete_dist_is_refused_before_any_remote_command(tmp_path):
    tree = _tree(tmp_path, dist=False)
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log, skip_build=True)

    assert result.returncode != 0
    assert 'frontend/dist is incomplete' in result.stderr
    assert _log(log) == ''


def test_backup_and_stop_precede_the_code_swap(tmp_path):
    tree = _tree(tmp_path)
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log, skip_build=True)

    assert result.returncode == 0, result.stderr
    recorded = _log(log)
    assert 'pg_restore --list' in recorded
    assert 'tar -czf' in recorded
    assert 'sudo systemctl stop' in recorded
    assert 'rsync -rltzv' in recorded
    assert recorded.index('--format=custom') < recorded.index('rsync -rltzv')
    assert recorded.index('tar -czf') < recorded.index('rsync -rltzv')
    assert recorded.index('sudo systemctl stop') < recorded.index('rsync -rltzv')


def test_migration_failure_does_not_start_the_services(tmp_path):
    tree = _tree(tmp_path)
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log, fail_on='init-db', skip_build=True)

    assert result.returncode != 0
    recorded = _log(log)
    assert 'systemctl start' not in recorded
    assert 'stay DOWN on purpose' in result.stderr
    assert 'hippo-backups/deploy-' in result.stderr


def test_stop_failure_aborts_before_swap_and_migration(tmp_path):
    tree = _tree(tmp_path)
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log, fail_on='systemctl stop', skip_build=True)

    assert result.returncode != 0
    recorded = _log(log)
    assert 'rsync -rltzv' not in recorded
    assert 'init-db' not in recorded
    assert 'deploy FAILED in stage: stop services' in result.stderr


def test_unfinished_backup_stops_before_anything_is_replaced(tmp_path):
    tree = _tree(tmp_path)
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log, fail_on='--format=custom', skip_build=True)

    assert result.returncode != 0
    assert 'is NOT complete' in result.stderr
    recorded = _log(log)
    assert 'sudo systemctl stop' not in recorded
    assert 'rsync -rltzv' not in recorded
