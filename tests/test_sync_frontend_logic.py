import unittest
from pathlib import Path

import quickjs


ROOT = Path(__file__).resolve().parent.parent
SYNC_JS = ROOT / 'static' / 'sync.js'


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
        context.eval(SYNC_JS.read_text(encoding='utf-8'))
        context.eval(f'var result = window.HippoSync.getActiveTaskAccountName({payload});')
        return context.eval('result')

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


if __name__ == '__main__':
    unittest.main()
