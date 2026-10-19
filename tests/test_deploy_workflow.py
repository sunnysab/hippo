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
import sys
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

# Records the libpq environment pg_dump would be handed, then leaves the dump
# file behind, so the mapping can be checked without a database.
PG_DUMP_RECORDER = """#!/bin/bash
env | grep -E '^PG[A-Z]*=' | sort > "$PG_DUMP_LOG"
file=''
while [ $# -gt 0 ]; do
    if [ "$1" = '--file' ]; then file="$2"; shift 2; else shift; fi
done
[ -n "$file" ] && : > "$file"
exit 0
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


def _tree(tmp_path: Path, *, dist: str = 'complete') -> Path:
    """The smallest checkout deploy.sh accepts, so nothing outside tmp_path is read.

    ``dist`` is 'complete' (index.html references files that exist), 'stale'
    (it references an asset the build no longer emits) or 'none'.
    """
    tree = tmp_path / 'tree'
    for relative in REQUIRED_FILES:
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('placeholder\n')
    if dist == 'none':
        return tree
    (tree / 'frontend/dist/assets').mkdir(parents=True)
    (tree / 'frontend/dist/assets/app-abc123.js').write_text('export {};\n')
    (tree / 'frontend/dist/assets/app-abc123.css').write_text('body {}\n')
    (tree / 'frontend/dist/favicon.svg').write_text('<svg/>\n')
    script = '/assets/app-old999.js' if dist == 'stale' else '/assets/app-abc123.js'
    (tree / 'frontend/dist/index.html').write_text(
        '<!doctype html>\n'
        '<html><head><link rel="icon" href="/favicon.svg">\n'
        '<link rel="stylesheet" href="/assets/app-abc123.css"></head>\n'
        f'<body><script type="module" src="{script}"></script></body></html>\n'
    )
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


def _embedded_dump_script() -> str:
    """The pg_dump snippet deploy.sh runs on the box, without its heredoc."""
    lines = DEPLOY_SH.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().endswith("<<'PY'"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == 'PY')
    return '\n'.join(lines[start + 1 : end]) + '\n'


def _run_embedded_dump(tmp_path: Path, dsn: str) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    """Run that snippet against a config.toml and a recording pg_dump."""
    work = tmp_path / 'remote'
    work.mkdir()
    (work / 'config.toml').write_text(f'[database]\ndsn = "{dsn}"\n')
    script = work / 'dump.py'
    script.write_text(_embedded_dump_script())
    fake_bin = tmp_path / 'fake-pg'
    fake_bin.mkdir()
    pg_dump = fake_bin / 'pg_dump'
    pg_dump.write_text(PG_DUMP_RECORDER)
    pg_dump.chmod(pg_dump.stat().st_mode | stat.S_IXUSR)

    recorded = tmp_path / 'pg_dump.env'
    # Drop any PG* the developer's shell exports, so only the DSN is under test.
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env['PATH'] = f'{fake_bin}{os.pathsep}{env["PATH"]}'
    env['PG_DUMP_LOG'] = str(recorded)
    env['PYTHONPATH'] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, str(script), str(work / 'database.dump')],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    params = dict(line.split('=', 1) for line in recorded.read_text().splitlines()) if recorded.exists() else {}
    return result, params


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
    tree = _tree(tmp_path, dist='none')
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


def test_dist_that_names_a_stale_asset_is_refused(tmp_path):
    tree = _tree(tmp_path, dist='stale')
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log, skip_build=True)

    assert result.returncode != 0
    assert 'app-old999.js' in result.stderr
    assert _log(log) == ''


def test_failure_never_points_at_overwriting_production(tmp_path):
    tree = _tree(tmp_path)
    fake_bin, log = _fake_bin(tmp_path)

    result = _run(tree, fake_bin, log, fail_on='init-db', skip_build=True)

    assert result.returncode != 0
    assert 'hippo.ops init-db' in result.stderr
    assert 'NEW database' in result.stderr
    assert 'pg_restore' not in result.stderr
    assert '--clean' not in result.stderr
    assert '--clean' not in DEPLOY_SH.read_text()


def test_url_dsn_reaches_pg_dump_through_the_environment(tmp_path):
    dsn = 'postgresql://hippo:s3cr3t%23pw@dbhost:5433/mp?sslmode=require&application_name=hippo'

    result, params = _run_embedded_dump(tmp_path, dsn)

    assert result.returncode == 0, result.stderr
    assert params['PGHOST'] == 'dbhost'
    assert params['PGPORT'] == '5433'
    assert params['PGUSER'] == 'hippo'
    assert params['PGPASSWORD'] == 's3cr3t#pw'
    assert params['PGDATABASE'] == 'mp'
    assert params['PGSSLMODE'] == 'require'
    assert params['PGAPPNAME'] == 'hippo'
    assert 's3cr3t' not in result.stdout + result.stderr
    assert dsn not in result.stdout + result.stderr


def test_keyword_value_dsn_reaches_pg_dump_through_the_environment(tmp_path):
    result, params = _run_embedded_dump(tmp_path, 'host=dbhost port=5433 user=hippo dbname=mp sslmode=verify-full')

    assert result.returncode == 0, result.stderr
    assert params['PGDATABASE'] == 'mp'
    assert params['PGSSLMODE'] == 'verify-full'
    assert 'PGPASSWORD' not in params


def test_libpq_option_without_an_environment_variable_is_refused(tmp_path):
    dsn = 'postgresql://hippo@dbhost/mp?keepalives=1'

    result, params = _run_embedded_dump(tmp_path, dsn)

    assert result.returncode != 0
    assert "parameter 'keepalives'" in result.stderr
    assert params == {}
    assert dsn not in result.stderr


def test_unparsable_dsn_is_refused_without_quoting_it(tmp_path):
    result, params = _run_embedded_dump(tmp_path, 'this is not a dsn')

    assert result.returncode != 0
    assert 'not a libpq connection string' in result.stderr
    assert params == {}
    assert 'this is not a dsn' not in result.stderr
