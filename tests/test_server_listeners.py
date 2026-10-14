import socket
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import typer

from hippo import cli, server


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'AF_UNIX is not available on this platform')
class ServerListenerTest(unittest.TestCase):
    def test_create_app_does_not_register_deprecated_event_handlers(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            app = server.create_app(
                static_dir=Path('frontend/dist'),
                enable_inprocess_sync=False,
            )

        self.assertIsNotNone(app)
        self.assertFalse(any(item.category is DeprecationWarning for item in caught))

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
                with (
                    patch.object(server, '_remove_stale_unix_socket', noop_remove),
                    self.assertRaisesRegex(RuntimeError, 'Failed to bind Unix socket'),
                ):
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
            # No TCP options: the socket is the listener, so uvicorn gets no host/port.
            self.assertNotIn('port', captured['config_kwargs'])

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

    def test_a_tcp_listener_needs_both_host_and_port(self) -> None:
        self.assertEqual(('0.0.0.0', 2000), cli._resolve_tcp_listener('0.0.0.0', 2000))
        self.assertEqual((None, None), cli._resolve_tcp_listener(None, None))
        # A port alone must not resurrect the listener the options no longer name.
        self.assertEqual((None, None), cli._resolve_tcp_listener(None, 2000))
        self.assertEqual((None, None), cli._resolve_tcp_listener('  ', 2000))

    def test_serve_only_binds_a_socket_when_no_host_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            uds_path = Path(tmpdir) / 'hippo.sock'
            captured: dict[str, object] = {}

            def fake_serve(**kwargs):
                captured.update(kwargs)

            with patch.object(cli, 'run_server', fake_serve):
                cli.serve(
                    host='127.0.0.1',
                    port=8000,
                    no_tcp=False,
                    unix_socket=uds_path,
                    unix_socket_mode='660',
                    static_dir=Path('frontend/dist'),
                    inprocess_sync=False,
                )

            self.assertEqual('127.0.0.1', captured['host'])
            self.assertEqual(8000, captured['port'])
