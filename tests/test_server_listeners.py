import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hippo import server


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'AF_UNIX is not available on this platform')
class ServerListenerTest(unittest.TestCase):
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

            for sock in sockets:
                sock.close()
