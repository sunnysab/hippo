import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hippo.server import create_app

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'settings' / 'SettingsPage.tsx'
ADMIN_SYNC_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'AdminSyncPage.tsx'
SYNC_SETTINGS_PANEL = ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'SyncSettingsPanel.tsx'
EMAIL_PANEL = ROOT / 'frontend' / 'src' / 'pages' / 'settings' / 'EmailPanel.tsx'
SYNC_RUN_HOOK = ROOT / 'frontend' / 'src' / 'hooks' / 'useSyncRun.ts'
GROUPS_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'groups' / 'GroupsPage.tsx'


class SettingsApiNamespaceTest(unittest.TestCase):
    def test_server_exposes_settings_routes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as static_dir,
            patch('hippo.server.configure_logging'),
            patch('hippo.server.init_telemetry', return_value=None),
            patch('hippo.server.instrument_app'),
        ):
            app = create_app(static_dir=static_dir, enable_inprocess_sync=False)
        route_map: dict[str, set[str]] = {}
        for route in app.routes:
            methods = {method.upper() for method in (getattr(route, 'methods', None) or set())}
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
        settings_page = SETTINGS_PAGE.read_text(encoding='utf-8')
        sync_page = ADMIN_SYNC_PAGE.read_text(encoding='utf-8')
        sync_settings_panel = SYNC_SETTINGS_PANEL.read_text(encoding='utf-8')
        email_panel = EMAIL_PANEL.read_text(encoding='utf-8')
        groups_page = GROUPS_PAGE.read_text(encoding='utf-8')
        sync_run_hook = SYNC_RUN_HOOK.read_text(encoding='utf-8')

        self.assertIn('/api/settings/status', sync_page)
        self.assertIn('/api/settings/tasks?limit=5&detail=true', sync_page)
        self.assertIn('/api/settings', settings_page)
        self.assertIn('/api/settings', sync_settings_panel)
        self.assertIn('/api/settings/test-email', email_panel)
        self.assertIn('useSyncRun', groups_page)
        self.assertIn('/api/settings/run', sync_run_hook)
        self.assertIn('/api/settings/admin/run', sync_run_hook)
        self.assertNotIn('/api/sync/settings', settings_page)
        self.assertNotIn('/api/sync/test-email', email_panel)
        self.assertNotIn('/api/sync/run', settings_page)
        self.assertNotIn('/api/sync/run', groups_page)


if __name__ == '__main__':
    unittest.main()
