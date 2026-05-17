import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_SHELL = ROOT / 'frontend' / 'src' / 'components' / 'AppShell.tsx'
TOP_BAR = ROOT / 'frontend' / 'src' / 'components' / 'TopBar.tsx'
ARTICLES_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'articles' / 'ArticlesPage.tsx'
ARTICLE_FILTERS = ROOT / 'frontend' / 'src' / 'pages' / 'articles' / 'ArticleFilters.tsx'
BATCH_ACTIONS = ROOT / 'frontend' / 'src' / 'pages' / 'groups' / 'BatchActions.tsx'
GROUPS_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'groups' / 'GroupsPage.tsx'
GROUP_HEADER = ROOT / 'frontend' / 'src' / 'pages' / 'groups' / 'GroupHeader.tsx'
MEDIA_QUERY_HOOK = ROOT / 'frontend' / 'src' / 'hooks' / 'useMediaQuery.ts'
I18N_ZH = ROOT / 'frontend' / 'src' / 'i18n' / 'zh-CN.json'


class FrontendRegressionTest(unittest.TestCase):
    def test_app_shell_clears_stale_topbar_meta_when_status_is_missing(self) -> None:
        source = APP_SHELL.read_text(encoding='utf-8')

        self.assertIn("else {\n        setLastLoginAt('');", source)
        self.assertIn("else {\n        setLastSyncAt('');", source)

    def test_viewport_logic_uses_shared_media_query_hook(self) -> None:
        hook_source = MEDIA_QUERY_HOOK.read_text(encoding='utf-8')
        articles_page = ARTICLES_PAGE.read_text(encoding='utf-8')
        article_filters = ARTICLE_FILTERS.read_text(encoding='utf-8')
        batch_actions = BATCH_ACTIONS.read_text(encoding='utf-8')

        self.assertIn('window.matchMedia', hook_source)
        self.assertNotIn('window.matchMedia', articles_page)
        self.assertNotIn('window.matchMedia', article_filters)
        self.assertNotIn('window.matchMedia', batch_actions)
        self.assertIn('useMediaQuery', articles_page)
        self.assertIn('useMediaQuery', article_filters)
        self.assertIn('useMediaQuery', batch_actions)

    def test_articles_page_limits_zero_delay_timers_to_route_sync(self) -> None:
        source = ARTICLES_PAGE.read_text(encoding='utf-8')

        self.assertLessEqual(source.count('window.setTimeout(() => {'), 1)
        self.assertIn('loadGroupOptions', source)
        self.assertIn('loadAccountOptions(filters.groupId)', source)
        self.assertIn('void loadArticles(nextFilters, true);', source)
        self.assertIn('void resolveArticleTarget();', source)

    def test_i18n_keys_exist_for_reader_controls_and_copy_feedback(self) -> None:
        translations = json.loads(I18N_ZH.read_text(encoding='utf-8'))
        articles_page = ARTICLES_PAGE.read_text(encoding='utf-8')

        self.assertIn('articles.copied', translations)
        self.assertIn('reader.serif', translations)
        self.assertNotIn("t('articles.copied', '已复制全文')", articles_page)

    def test_topbar_uses_navigation_links_instead_of_tablist_role(self) -> None:
        source = TOP_BAR.read_text(encoding='utf-8')

        self.assertIn('NavLink', source)
        self.assertNotIn('role="tablist"', source)
        self.assertNotIn('useNavigate', source)
        self.assertNotIn('onClick={() => navigate(tab.path)}', source)

    def test_batch_actions_uses_toasts_instead_of_alerts(self) -> None:
        source = BATCH_ACTIONS.read_text(encoding='utf-8')

        self.assertNotIn('alert(', source)
        self.assertIn('showToast', source)

    def test_group_create_and_rename_do_not_use_native_prompt(self) -> None:
        groups_page = GROUPS_PAGE.read_text(encoding='utf-8')
        group_header = GROUP_HEADER.read_text(encoding='utf-8')

        self.assertNotIn('prompt(', groups_page)
        self.assertNotIn('prompt(', group_header)

    def test_group_delete_does_not_use_native_confirm(self) -> None:
        source = GROUP_HEADER.read_text(encoding='utf-8')

        self.assertNotIn('confirm(', source)

    def test_groups_page_syncs_selected_group_with_route_query(self) -> None:
        source = GROUPS_PAGE.read_text(encoding='utf-8')

        self.assertIn("useSearchParams", source)
        self.assertIn("searchParams.get('group')", source)
        self.assertIn("nextParams.set('group', nextGroup)", source)
        self.assertIn("dispatch({ type: 'SELECT_GROUP', groupId: nextGroupId })", source)


if __name__ == '__main__':
    unittest.main()
