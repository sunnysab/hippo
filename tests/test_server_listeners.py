import asyncio
import socket
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from hippo import server, sync_worker


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


class EntrypointTest(unittest.TestCase):
    def test_parse_octal_mode_rejects_negative_value(self) -> None:
        with self.assertRaisesRegex(ValueError, 'Unix socket mode must be between 000 and 777'):
            server._parse_octal_mode('-1')

    def test_parse_octal_mode_rejects_bits_outside_permission_range(self) -> None:
        with self.assertRaisesRegex(ValueError, 'Unix socket mode must be between 000 and 777'):
            server._parse_octal_mode('1777')

    def test_help_does_not_read_settings_or_bind_a_listener(self) -> None:
        with (
            patch.object(server, 'serve') as serve,
            patch.object(server, 'get_settings') as get_settings,
            self.assertRaises(SystemExit) as ctx,
        ):
            server.main(['--help'])

        self.assertEqual(0, ctx.exception.code)
        serve.assert_not_called()
        get_settings.assert_not_called()

    def test_invalid_port_does_not_bind_a_listener(self) -> None:
        with (
            patch.object(server, 'serve') as serve,
            self.assertRaises(SystemExit) as ctx,
        ):
            server.main(['--host', '127.0.0.1', '--port', '70000'])

        self.assertEqual(2, ctx.exception.code)
        serve.assert_not_called()

    def test_no_listener_does_not_read_settings_or_bind_a_listener(self) -> None:
        with (
            patch.object(server, 'serve') as serve,
            patch.object(server, 'get_settings') as get_settings,
            self.assertRaises(SystemExit) as ctx,
        ):
            server.main([])

        self.assertEqual(2, ctx.exception.code)
        serve.assert_not_called()
        get_settings.assert_not_called()

    def test_host_only_does_not_bind_a_listener(self) -> None:
        with (
            patch.object(server, 'serve') as serve,
            self.assertRaises(SystemExit) as ctx,
        ):
            server.main(['--host', '127.0.0.1'])

        self.assertEqual(2, ctx.exception.code)
        serve.assert_not_called()

    def test_port_only_does_not_bind_a_listener(self) -> None:
        with (
            patch.object(server, 'serve') as serve,
            self.assertRaises(SystemExit) as ctx,
        ):
            server.main(['--port', '8000'])

        self.assertEqual(2, ctx.exception.code)
        serve.assert_not_called()

    def test_host_and_port_without_socket_still_binds(self) -> None:
        captured: dict[str, object] = {}

        def fake_serve(**kwargs):
            captured.update(kwargs)

        with patch.object(server, 'serve', fake_serve):
            server.main(['--host', '127.0.0.1', '--port', '8000'])

        self.assertEqual('127.0.0.1', captured['host'])
        self.assertEqual(8000, captured['port'])
        self.assertIsNone(captured['unix_socket'])

    def test_socket_only_still_binds_without_tcp_options(self) -> None:
        captured: dict[str, object] = {}

        def fake_serve(**kwargs):
            captured.update(kwargs)

        with patch.object(server, 'serve', fake_serve):
            server.main(['--unix-socket', '/run/hippo/hippo.sock'])

        self.assertIsNone(captured['host'])
        self.assertIsNone(captured['port'])
        self.assertEqual(Path('/run/hippo/hippo.sock'), captured['unix_socket'])

    def test_blank_socket_does_not_read_settings_or_bind_a_listener(self) -> None:
        with (
            patch.object(server, 'serve') as serve,
            patch.object(server, 'get_settings') as get_settings,
            self.assertRaises(SystemExit) as ctx,
        ):
            server.main(['--no-tcp', '--unix-socket', '  '])

        self.assertEqual(2, ctx.exception.code)
        serve.assert_not_called()
        get_settings.assert_not_called()

    def test_serve_requires_unix_socket_when_tcp_is_disabled(self) -> None:
        with (
            patch.object(server, 'serve') as serve,
            self.assertRaises(SystemExit) as ctx,
        ):
            server.main(['--no-tcp'])

        self.assertEqual(2, ctx.exception.code)
        serve.assert_not_called()

    def test_a_tcp_listener_needs_both_host_and_port(self) -> None:
        self.assertEqual(('0.0.0.0', 2000), server._resolve_tcp_listener('0.0.0.0', 2000))
        self.assertEqual((None, None), server._resolve_tcp_listener(None, None))
        # A port alone must not resurrect the listener the options no longer name.
        self.assertEqual((None, None), server._resolve_tcp_listener(None, 2000))
        self.assertEqual((None, None), server._resolve_tcp_listener('  ', 2000))

    def test_main_only_binds_a_socket_when_no_host_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            uds_path = Path(tmpdir) / 'hippo.sock'
            captured: dict[str, object] = {}

            def fake_serve(**kwargs):
                captured.update(kwargs)

            with patch.object(server, 'serve', fake_serve):
                server.main(
                    [
                        '--host',
                        '127.0.0.1',
                        '--port',
                        '8000',
                        '--unix-socket',
                        str(uds_path),
                        '--unix-socket-mode',
                        '660',
                    ]
                )

            self.assertEqual('127.0.0.1', captured['host'])
            self.assertEqual(8000, captured['port'])
            self.assertEqual(uds_path, captured['unix_socket'])
            self.assertEqual(0o660, captured['unix_socket_mode'])

    def test_main_drops_the_tcp_listener_with_no_tcp(self) -> None:
        captured: dict[str, object] = {}

        def fake_serve(**kwargs):
            captured.update(kwargs)

        with patch.object(server, 'serve', fake_serve):
            server.main(['--no-tcp', '--unix-socket', '/run/hippo/hippo.sock'])

        self.assertIsNone(captured['host'])
        self.assertIsNone(captured['port'])


class WorkerEntrypointTest(unittest.TestCase):
    def test_help_does_not_start_the_worker(self) -> None:
        with (
            patch('hippo.sync_worker.run_sync_worker') as run_worker,
            patch('hippo.sync_worker.asyncio.run') as asyncio_run,
            self.assertRaises(SystemExit) as ctx,
        ):
            sync_worker.main(['--help'])

        self.assertEqual(0, ctx.exception.code)
        run_worker.assert_not_called()
        asyncio_run.assert_not_called()

    def test_rejects_a_poll_interval_below_the_floor(self) -> None:
        with (
            patch('hippo.sync_worker.run_sync_worker') as run_worker,
            patch('hippo.sync_worker.asyncio.run') as asyncio_run,
            self.assertRaises(SystemExit) as ctx,
        ):
            sync_worker.main(['--poll-interval', '0.1'])

        self.assertEqual(2, ctx.exception.code)
        run_worker.assert_not_called()
        asyncio_run.assert_not_called()

    def test_rejects_a_non_finite_poll_interval(self) -> None:
        for value in ('nan', 'inf', '-inf'):
            with self.subTest(value=value):
                with (
                    patch('hippo.sync_worker.run_sync_worker') as run_worker,
                    patch('hippo.sync_worker.asyncio.run') as asyncio_run,
                    self.assertRaises(SystemExit) as ctx,
                ):
                    sync_worker.main(['--poll-interval', value])

                self.assertEqual(2, ctx.exception.code)
                run_worker.assert_not_called()
                asyncio_run.assert_not_called()

    def test_passes_the_poll_interval_and_closes_the_pool_on_a_live_loop(self) -> None:
        captured: dict[str, object] = {}
        loop_states: list[bool] = []

        async def fake_run_sync_worker(**kwargs) -> None:
            captured.update(kwargs)
            loop_states.append(asyncio.get_running_loop().is_running())

        async def fake_close_pool() -> None:
            # close_pool must run while the loop is still alive: asyncio.run tears
            # the loop down with a connection in flight, which hangs the process.
            loop_states.append(asyncio.get_running_loop().is_running())

        with (
            patch('hippo.sync_worker.run_sync_worker', fake_run_sync_worker),
            patch('hippo.sync_worker.close_pool', fake_close_pool),
        ):
            code = sync_worker.main(['--poll-interval', '2.5'])

        self.assertEqual(0, code)
        self.assertEqual({'poll_interval': 2.5}, captured)
        self.assertEqual([True, True], loop_states)

    def test_closes_the_pool_even_when_the_worker_raises(self) -> None:
        closed: list[bool] = []

        async def fake_run_sync_worker(**kwargs) -> None:
            raise RuntimeError('worker failed')

        async def fake_close_pool() -> None:
            closed.append(asyncio.get_running_loop().is_running())

        with (
            patch('hippo.sync_worker.run_sync_worker', fake_run_sync_worker),
            patch('hippo.sync_worker.close_pool', fake_close_pool),
            self.assertRaises(RuntimeError),
        ):
            sync_worker.main(['--poll-interval', '2.5'])

        self.assertEqual([True], closed, 'a failure must still release the pool on a live loop')
