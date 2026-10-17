"""Static checks on the knowledge import worker's unit and the runtime image.

These tests read two repository files as text - ``hippo-knowledge-worker.service``
and ``Dockerfile`` - and parse the unit as a systemd-style ini file. They never
start systemd, Docker, PostgreSQL or the worker, never build an image and never
prove that a system tool exists inside one. What they pin down is the shape of
the two files: the unit runs the documented module with the documented
environment, stops gracefully, does not depend on the WeChat daemon, and carries
no credentials; the image still installs the parser's external tools and still
defaults to the web entrypoint.
"""

from __future__ import annotations

import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / 'hippo-knowledge-worker.service'
DOCKERFILE_PATH = ROOT / 'Dockerfile'

#: The packages the knowledge/media pipeline actually shells out to. The Docker
#: test asserts these are requested; it cannot assert they are installed.
_EXPECTED_APT_PACKAGES = ('build-essential', 'libpq-dev', 'poppler-utils', 'util-linux', 'ffmpeg')

#: Substrings that would mean a deployment value leaked into the unit instead
#: of ``config.toml``. Only real directive lines are checked - comments may
#: mention them.
_FORBIDDEN_UNIT_TEXT = (
    'dsn',
    'postgresql://',
    'secret',
    'access_key',
    'token',
    'password',
    'environmentfile',
    'weixin',
    'daemon',
)


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    # Keys keep their case: systemd directives are case-sensitive.
    parser.optionxform = str
    with UNIT_PATH.open(encoding='utf-8') as handle:
        parser.read_file(handle)
    return parser


def _directive_lines() -> list[str]:
    """Non-comment, non-blank lines of the unit, in file order."""
    lines = [line.strip() for line in UNIT_PATH.read_text(encoding='utf-8').splitlines()]
    return [line for line in lines if line and not line.startswith(('#', ';'))]


def test_the_unit_runs_the_knowledge_worker_module() -> None:
    service = _unit()['Service']

    assert service['Type'] == 'simple'
    assert service['User'] == 'sab'
    assert service['WorkingDirectory'] == '/home/sab/hippo'
    assert service['ExecStart'] == '/home/sab/hippo/.venv/bin/python -m hippo.knowledge.worker'


def test_the_unit_keeps_the_config_out_of_the_environment() -> None:
    service = _unit()['Service']
    lines = _directive_lines()

    assert service['Environment'] == 'PYTHONUNBUFFERED=1'
    assert [line for line in lines if line.startswith('Environment=')] == ['Environment=PYTHONUNBUFFERED=1']
    for line in lines:
        for needle in _FORBIDDEN_UNIT_TEXT:
            assert needle not in line.lower(), f'{needle!r} must not appear in {line!r}; config.toml is the only source'


def test_the_unit_only_requires_network_and_database() -> None:
    unit = _unit()['Unit']

    after = unit['After'].split()
    assert 'network.target' in after
    assert 'postgresql.service' in after
    assert not any('weixin' in entry for entry in after)


def test_the_unit_stops_gracefully_and_restarts_only_after_a_failure() -> None:
    service = _unit()['Service']

    assert service['KillSignal'] == 'SIGINT'
    assert service['Restart'] == 'on-failure'
    assert int(service['RestartSec']) > 0
    # A fixed, finite grace period: asyncio cancels the loop and the SDK gets
    # time to finish; a lease left behind is recovered by the worker itself.
    stop_seconds = int(service['TimeoutStopSec'])
    assert 0 < stop_seconds <= 360
    assert _unit()['Install']['WantedBy'] == 'multi-user.target'


def _runtime_apt_line() -> str:
    """The whole apt-get install command, which the file wraps over two lines."""
    lines = DOCKERFILE_PATH.read_text(encoding='utf-8').splitlines()
    start = next(index for index, line in enumerate(lines) if 'apt-get install' in line)
    block = []
    for line in lines[start:]:
        block.append(line)
        if not line.rstrip().endswith('\\'):
            break
    return '\n'.join(block)


def test_the_image_installs_the_parser_tools_and_keeps_the_web_entrypoint() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding='utf-8')

    apt_block = _runtime_apt_line()
    for package in _EXPECTED_APT_PACKAGES:
        assert package in apt_block, f'{package} is used at run time and must be requested by the image'
    assert 'node:24-slim' in dockerfile
    assert 'python:3.14-slim' in dockerfile
    assert 'uv sync --frozen --no-dev' in dockerfile
    assert 'ENTRYPOINT ["/app/.venv/bin/python", "-m", "hippo.server"]' in dockerfile
