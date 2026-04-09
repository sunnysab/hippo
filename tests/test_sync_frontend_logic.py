import unittest
from pathlib import Path

import quickjs


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / 'static' / 'app.js'
SETTINGS_JS = ROOT / 'static' / 'settings.js'
SETTINGS_HTML = ROOT / 'static' / 'pages' / 'settings.html'


class SyncFrontendLogicTest(unittest.TestCase):
    def run_account_name_helper(self, payload: dict) -> str:
        context = quickjs.Context()
        context.eval(
            """
            var window = {
              Hippo: {
                state: {},
                $: function () { return null; },
                apiGet: function () {},
                apiSend: function () {},
                t: function (_key, fallback) { return fallback; },
                activateTab: function () {},
                showToast: function () {}
              }
            };
            """
        )
        context.eval(SETTINGS_JS.read_text(encoding='utf-8'))
        context.eval(f'var result = window.HippoSettings.getActiveTaskAccountName({payload});')
        return context.eval('result')

    def build_sync_context(self) -> quickjs.Context:
        context = quickjs.Context()
        context.eval(
            """
            var window = {
              Hippo: {
                state: {},
                $: function () { return null; },
                apiGet: function () {},
                apiSend: function () {},
                t: function (_key, fallback) { return fallback; },
                activateTab: function () {},
                showToast: function () {}
              }
            };
            """
        )
        context.eval(SETTINGS_JS.read_text(encoding='utf-8'))
        return context

    def test_running_task_without_account_uses_preparing_copy(self) -> None:
        result = self.run_account_name_helper(
            """{
              status: 'running',
              accounts_total: 0,
              accounts_done: 0,
              current_account: null,
              phase: null,
              last_log: null
            }"""
        )

        self.assertEqual('Preparing sync task', result)

    def test_sync_module_exposes_start_login_for_global_actions(self) -> None:
        context = self.build_sync_context()

        self.assertEqual('function', context.eval('typeof window.HippoSettings.startLogin'))

    def test_settings_page_wires_article_exclude_keywords_field(self) -> None:
        settings_html = SETTINGS_HTML.read_text(encoding='utf-8')
        settings_js = SETTINGS_JS.read_text(encoding='utf-8')

        self.assertIn('id="sync-article-exclude-keywords"', settings_html)
        self.assertIn('article_exclude_keywords', settings_js)
        self.assertIn("#sync-article-exclude-keywords", settings_js)

    def test_global_banner_login_click_switches_to_settings_and_starts_login(self) -> None:
        context = quickjs.Context()
        context.eval(
            """
            var bannerButton = {
              listeners: {},
              addEventListener: function (type, handler) {
                this.listeners[type] = handler;
              },
              click: function () {
                if (this.listeners.click) {
                  return this.listeners.click();
                }
              }
            };
            var refreshButton = {
              addEventListener: function () {}
            };
            var nodes = {
              '#btn-refresh': refreshButton,
              '#btn-banner-login': bannerButton
            };
            var document = {
              querySelector: function (selector) {
                return nodes[selector] || null;
              },
              querySelectorAll: function () {
                return [];
              },
              body: {
                appendChild: function () {}
              },
              createElement: function () {
                return {
                  style: {},
                  select: function () {},
                  remove: function () {}
                };
              },
              execCommand: function () {
                return true;
              }
            };
            var window = {
              location: { hash: '#/groups' },
              HippoSettings: {
                startLoginCalls: 0,
                startLogin: function () {
                  this.startLoginCalls += 1;
                }
              },
              addEventListener: function () {},
              history: {
                replaceState: function () {}
              }
            };
            var navigator = {};
            var history = window.history;
            var fetch = function () {};
            var setTimeout = function () { return 1; };
            var clearTimeout = function () {};
            var setInterval = function () { return 1; };
            var clearInterval = function () {};
            """
        )
        context.eval(
            APP_JS.read_text(encoding='utf-8')
            + """
            window.__test = {
              bindGlobalEvents: bindGlobalEvents
            };
            """
        )
        context.eval('window.__test.bindGlobalEvents()')
        context.eval('bannerButton.click()')

        self.assertEqual('#/settings', context.eval('window.location.hash'))
        self.assertEqual(1, context.eval('window.HippoSettings.startLoginCalls'))


if __name__ == '__main__':
    unittest.main()
