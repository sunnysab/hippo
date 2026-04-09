import unittest
from pathlib import Path

from hippo.server import router


ROOT = Path(__file__).resolve().parent.parent
SETTINGS_JS = ROOT / 'static' / 'settings.js'
GROUPS_JS = ROOT / 'static' / 'groups.js'


class SettingsApiNamespaceTest(unittest.TestCase):
    def test_server_exposes_settings_routes(self) -> None:
        route_map: dict[str, set[str]] = {}
        for route in router.routes:
            methods = {method.upper() for method in (route.methods or set())}
            route_map.setdefault(route.path, set()).update(methods)

        self.assertIn('/api/settings/status', route_map)
        self.assertIn('GET', route_map['/api/settings/status'])
        self.assertIn('/api/settings/tasks', route_map)
        self.assertIn('GET', route_map['/api/settings/tasks'])
        self.assertIn('/api/settings/tasks/{task_id}', route_map)
        self.assertIn('GET', route_map['/api/settings/tasks/{task_id}'])
        self.assertIn('/api/settings', route_map)
        self.assertIn('GET', route_map['/api/settings'])
        self.assertIn('PATCH', route_map['/api/settings'])
        self.assertIn('/api/settings/test-email', route_map)
        self.assertIn('POST', route_map['/api/settings/test-email'])
        self.assertIn('/api/settings/run', route_map)
        self.assertIn('POST', route_map['/api/settings/run'])
        self.assertNotIn('/api/sync', route_map)
        self.assertNotIn('/api/sync/tasks', route_map)
        self.assertNotIn('/api/sync/tasks/{task_id}', route_map)
        self.assertNotIn('/api/sync/settings', route_map)
        self.assertNotIn('/api/sync/test-email', route_map)
        self.assertNotIn('/api/sync/run', route_map)

    def test_frontend_uses_settings_api_namespace(self) -> None:
        settings_js = SETTINGS_JS.read_text(encoding='utf-8')
        groups_js = GROUPS_JS.read_text(encoding='utf-8')

        self.assertIn('/api/settings/status', settings_js)
        self.assertIn('/api/settings/tasks?limit=5&detail=true', settings_js)
        self.assertIn('/api/settings', settings_js)
        self.assertIn('/api/settings/test-email', settings_js)
        self.assertIn('/api/settings/run', settings_js)
        self.assertIn('/api/settings/run', groups_js)
        self.assertNotIn('/api/sync/settings', settings_js)
        self.assertNotIn('/api/sync/test-email', settings_js)
        self.assertNotIn('/api/sync/run', settings_js)
        self.assertNotIn('/api/sync/run', groups_js)


if __name__ == '__main__':
    unittest.main()
