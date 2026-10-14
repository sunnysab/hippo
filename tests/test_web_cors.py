"""Cross-origin access is closed until an origin is named.

The SPA is served from the same origin as the API, so a wildcard would only ever
be a hole -- and one that becomes dangerous the moment credentials are allowed.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from hippo import config, server


class CorsTest(unittest.TestCase):
    def _app(self):
        return TestClient(server.create_app(static_dir=Path('frontend/dist'), enable_inprocess_sync=False))

    def test_a_preflight_is_refused_when_no_origin_is_named(self) -> None:
        # A preflight is answered by the CORS middleware itself, so this stays
        # off the database.
        response = self._app().options(
            '/api/login',
            headers={'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'GET'},
        )

        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_only_the_named_origins_are_answered(self) -> None:
        settings = config.Settings(web=config.WebSettings(allowed_origins=('https://ok.example',)))

        with patch.object(config, '_SETTINGS', settings):
            client = self._app()
            allowed = client.options(
                '/api/login',
                headers={'Origin': 'https://ok.example', 'Access-Control-Request-Method': 'GET'},
            )
            rejected = client.options(
                '/api/login',
                headers={'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'GET'},
            )

        self.assertEqual('https://ok.example', allowed.headers.get('access-control-allow-origin'))
        self.assertNotIn('access-control-allow-origin', rejected.headers)
