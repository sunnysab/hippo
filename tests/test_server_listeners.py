import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import typer

from hippo import cli
from hippo import server


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'AF_UNIX is not available on this platform')
class ServerListenerTest(unittest.TestCase):
    def test_remove_stale_unix_socket_rejects_active_listener(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            uds_path = Path(tmpdir) / 'hippo.sock'
            active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            active.bind(str(uds_path))
            active.listen(1)

            try:
                with self.assertRaisesRegex(RuntimeError, 'Unix socket path is already in use'):
                    server._remove_stale_unix_socket(uds_path)
                self.assertTrue(uds_path.exists())
            finally:
                active.close()

    def test_create_unix_listen_socket_keeps_existing_path_when_bind_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            uds_path = Path(tmpdir) / 'hippo.sock'
            active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            active.bind(str(uds_path))
            active.listen(1)

            def noop_remove(path: Path) -> None:
                return None

            try:
                with patch.object(server, '_remove_stale_unix_socket', noop_remove):
                    with self.assertRaisesRegex(RuntimeError, 'Failed to bind Unix socket'):
                        server._create_unix_listen_socket(uds_path, 0o660)
                self.assertTrue(uds_path.exists())
            finally:
                active.close()

    def test_build_listen_sockets_supports_tcp_and_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            uds_path = Path(tmpdir) / 'hippo.sock'

            sockets = server._build_listen_sockets(
                host='127.0.0.1',
                port=0,
                unix_socket=uds_path,
                unix_socket_mode=0o660,
            )

            self.assertEqual(2, len(sockets))
            self.assertTrue(any(sock.family == socket.AF_INET for sock in sockets))
            self.assertTrue(any(sock.family == socket.AF_UNIX for sock in sockets))
            self.assertTrue(uds_path.exists())
            self.assertEqual(0o660, uds_path.stat().st_mode & 0o777)

            for sock in sockets:
                sock.close()

    def test_build_listen_sockets_requires_at_least_one_listener(self) -> None:
        with self.assertRaisesRegex(RuntimeError, 'At least one listener must be configured'):
            server._build_listen_sockets(
                host=None,
                port=None,
                unix_socket=None,
            )

    def test_build_listen_sockets_reports_missing_unix_socket_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            uds_path = Path(tmpdir) / 'missing' / 'hippo.sock'

            with self.assertRaisesRegex(RuntimeError, 'Unix socket parent directory does not exist'):
                server._build_listen_sockets(
                    host=None,
                    port=None,
                    unix_socket=uds_path,
                )

    def test_serve_passes_bound_sockets_to_uvicorn_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            uds_path = Path(tmpdir) / 'hippo.sock'
            captured: dict[str, object] = {}

            class DummyConfig:
                def __init__(self, app, **kwargs):
                    captured['app'] = app
                    captured['config_kwargs'] = kwargs

            class DummyServer:
                def __init__(self, config):
                    captured['config'] = config

                def run(self, sockets=None):
                    captured['sockets'] = sockets

            with patch('uvicorn.Config', DummyConfig), patch('uvicorn.Server', DummyServer):
                server.serve(
                    host='127.0.0.1',
                    port=0,
                    unix_socket=uds_path,
                    unix_socket_mode=0o660,
                )

            sockets = captured['sockets']
            self.assertIsInstance(sockets, list)
            self.assertEqual(2, len(sockets))
            self.assertEqual('warning', captured['config_kwargs']['log_level'])
            self.assertEqual(0, captured['config_kwargs']['port'])

            for sock in sockets:
                sock.close()


class CliServeTest(unittest.TestCase):
    def test_parse_octal_mode_rejects_negative_value(self) -> None:
        with self.assertRaisesRegex(typer.BadParameter, 'Unix socket mode must be between 000 and 777'):
            cli._parse_octal_mode('-1')

    def test_parse_octal_mode_rejects_bits_outside_permission_range(self) -> None:
        with self.assertRaisesRegex(typer.BadParameter, 'Unix socket mode must be between 000 and 777'):
            cli._parse_octal_mode('1777')

    def test_serve_requires_unix_socket_when_tcp_is_disabled(self) -> None:
        with self.assertRaisesRegex(typer.BadParameter, '--unix-socket is required when --no-tcp is set'):
            cli.serve(
                host='127.0.0.1',
                port=8000,
                no_tcp=True,
                unix_socket=None,
                unix_socket_mode='660',
                static_dir=Path('frontend/dist'),
                inprocess_sync=False,
            )
