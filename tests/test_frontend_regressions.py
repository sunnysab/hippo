import json
import re
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
ARTICLE_FILTER_HOOK = ROOT / 'frontend' / 'src' / 'hooks' / 'useArticleFilters.ts'
ARTICLE_READER_HOOK = ROOT / 'frontend' / 'src' / 'hooks' / 'useArticleReader.ts'
VIEW_RAIL = ROOT / 'frontend' / 'src' / 'pages' / 'articles' / 'ViewRail.tsx'
I18N_ZH = ROOT / 'frontend' / 'src' / 'i18n' / 'zh-CN.json'
ADMIN_LAYOUT = ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'AdminLayout.tsx'
ADMIN_SOURCE_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'AdminSourcePage.tsx'
ADMIN_SYNC_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'AdminSyncPage.tsx'
ADMIN_APP = ROOT / 'frontend' / 'src' / 'App.tsx'
SETTINGS_CSS = ROOT / 'frontend' / 'src' / 'styles' / 'settings.css'
ARTICLES_CSS = ROOT / 'frontend' / 'src' / 'styles' / 'articles.css'
BRAND_MARK = ROOT / 'frontend' / 'src' / 'components' / 'BrandMark.tsx'
FAVICON = ROOT / 'frontend' / 'public' / 'favicon.svg'
LOGIN_PAGE = ROOT / 'frontend' / 'src' / 'pages' / 'auth' / 'LoginPage.tsx'
SYNC_RUN_HOOK = ROOT / 'frontend' / 'src' / 'hooks' / 'useSyncRun.ts'
ADMIN_SYNC_SECTION = ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'AdminSyncSection.tsx'
SYNC_SETTINGS_PANEL = ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'SyncSettingsPanel.tsx'


def _mark_path_data(source: str) -> str:
    """The `d` attribute, whitespace-normalised, from the mark or the favicon."""
    match = re.search(r'd="(.*?)"', source, re.S) or re.search(r'MARK_PATH = `(.*?)`', source, re.S)
    if match is None:
        raise AssertionError('no mark path found')
    return ' '.join(match.group(1).split())


class FrontendRegressionTest(unittest.TestCase):
    def test_app_shell_refreshes_sync_status_and_meta(self) -> None:
        source = APP_SHELL.read_text(encoding='utf-8')

        # 同步服务状态是语义化的 state，文案由 TopBar 负责渲染
        self.assertIn("setSyncStatus('online');", source)
        self.assertNotIn('lastLoginAt', source)
        self.assertIn("else {\n        setLastSyncAt('');", source)

    def test_the_daemon_status_belongs_to_the_console_only(self) -> None:
        # 全站登录/同步状态是管理台的事：普通读者不请求它，也没有横幅可关。
        shell = APP_SHELL.read_text(encoding='utf-8')

        self.assertIn('if (!isAdmin) return;', shell)
        banner_at = shell.index('id="status-banner"')
        self.assertIn('{isAdmin ? (', shell[:banner_at])

    def test_viewport_logic_uses_shared_media_query_hook(self) -> None:
        # 视口判断收敛到 hooks：页面组件里不再直接碰 window.matchMedia
        hook_source = MEDIA_QUERY_HOOK.read_text(encoding='utf-8')
        self.assertIn('window.matchMedia', hook_source)
        for page in (ARTICLES_PAGE, ARTICLE_FILTERS, BATCH_ACTIONS):
            self.assertNotIn('window.matchMedia', page.read_text(encoding='utf-8'))
        reader_hook = ARTICLE_READER_HOOK.read_text(encoding='utf-8')
        filters_hook = ARTICLE_FILTER_HOOK.read_text(encoding='utf-8')
        self.assertIn('useMediaQuery', reader_hook)
        self.assertIn('useMediaQuery', filters_hook)

    def test_articles_page_defers_data_loading_to_hooks(self) -> None:
        # 数据加载搬进了 useArticleFilters，页面只负责组合
        source = ARTICLES_PAGE.read_text(encoding='utf-8')
        filters_hook = ARTICLE_FILTER_HOOK.read_text(encoding='utf-8')

        self.assertLessEqual(source.count('window.setTimeout(() => {'), 1)
        self.assertIn('loadGroupOptions', filters_hook)
        self.assertIn('loadAccountOptions(filters.groupId)', filters_hook)
        self.assertIn('void loadArticles(nextFilters, true);', filters_hook)
        self.assertIn('void resolveArticleTarget();', filters_hook)

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

    def test_admin_navigation_follows_the_source_model(self) -> None:
        # 来源是一等公民：凭证、代理、扫码登录都归到各自的来源页，
        # 共享的转写参数单独一页，不再挂在某个来源下。
        layout = ADMIN_LAYOUT.read_text(encoding='utf-8')
        app = ADMIN_APP.read_text(encoding='utf-8')

        self.assertIn("path: '/admin/source'", layout)
        self.assertIn("path: '/admin/transcribe'", layout)
        self.assertNotIn("path: '/admin/login'", layout)
        self.assertNotIn("path: '/admin/media'", layout)
        self.assertIn("path=\"source/:source\" element={<SourceDetailPage />}", app)
        # 旧地址仍然可达：书签与横幅不该撞上空白页。
        self.assertIn('path="login" element={<Navigate to="/admin/source/weixin" replace />}', app)
        self.assertIn('path="media" element={<Navigate to="/admin/transcribe" replace />}', app)

    def test_the_sync_console_stacks_in_one_column(self) -> None:
        # 两栏在宽屏留下半屏空白，窄屏又塌成一栏：同步页改成单列。
        admin_css = (ROOT / 'frontend' / 'src' / 'styles' / 'settings.css').read_text(encoding='utf-8')
        sync_page = ADMIN_SYNC_PAGE.read_text(encoding='utf-8')

        self.assertNotIn('sync-columns', admin_css)
        self.assertNotIn('sync-columns', sync_page)
        self.assertIn('admin-page', sync_page)

    def test_admin_source_page_lists_what_the_backend_reports(self) -> None:
        source = ADMIN_SOURCE_PAGE.read_text(encoding='utf-8')

        # 来源清单来自后端，前端不再自己维护一份。
        self.assertIn('/api/admin/source', (ROOT / 'frontend' / 'src' / 'pages' / 'admin' / 'sources.ts').read_text(encoding='utf-8'))
        self.assertIn('runSync({ source, siteWide: true })', source)

    def test_brand_mark_is_one_colour_and_matches_the_favicon(self) -> None:
        # 顶栏、登录页和浏览器标签共用一份几何：不比对就会各自漂移。
        mark = BRAND_MARK.read_text(encoding='utf-8')
        favicon = FAVICON.read_text(encoding='utf-8')

        self.assertIn('fill="currentColor"', mark)
        self.assertIn('fill="currentColor"', favicon)
        # 单色：除了 currentColor 之外不再有写死的填充色。
        self.assertNotIn('fill="#', mark)
        self.assertNotIn('fill="#', favicon)
        self.assertEqual(_mark_path_data(favicon), _mark_path_data(mark))

    def test_the_brand_badge_uses_the_mark_everywhere(self) -> None:
        top_bar = TOP_BAR.read_text(encoding='utf-8')
        login_page = LOGIN_PAGE.read_text(encoding='utf-8')

        for source in (top_bar, login_page):
            self.assertIn('<BrandMark className="brand-mark" />', source)
            self.assertNotIn('>H</span>', source)
        self.assertIn('favicon.svg', (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8'))

    def test_brand_subtitle_describes_a_personal_knowledge_platform(self) -> None:
        translations = json.loads(I18N_ZH.read_text(encoding='utf-8'))

        self.assertEqual(translations['brand.subtitle'], '个人信息与知识平台')

    def test_the_view_rail_keeps_the_group_subscription_counts(self) -> None:
        # /api/group 会返回 account_count：hook 要是把它丢了，分组行就没有数字可显示。
        hook = ARTICLE_FILTER_HOOK.read_text(encoding='utf-8')
        rail = VIEW_RAIL.read_text(encoding='utf-8')

        self.assertIn('count: group.account_count', hook)
        self.assertIn('articles.railSubscriptions', rail)
        # 订阅变化会 emitRefresh，这一栏要跟着重取。
        self.assertIn('onRefresh', rail)

    def test_article_thumbnails_keep_a_fixed_sixteen_by_nine_box(self) -> None:
        # 封面在列表里必须一样大：按图片自己的比例排，每一行都会不一样高。
        css = ARTICLES_CSS.read_text(encoding='utf-8')
        block = re.search(r'^\.article-thumb \{(.*?)\}', css, re.S | re.M)
        self.assertIsNotNone(block)
        rule = block.group(1)  # type: ignore[union-attr]

        self.assertIn('aspect-ratio: 16 / 9;', rule)
        self.assertIn('object-fit: cover;', rule)
        self.assertNotIn('aspect-ratio: auto', rule)

    def test_one_hook_owns_manual_runs_and_their_task_polling(self) -> None:
        hook = SYNC_RUN_HOOK.read_text(encoding='utf-8')

        # 手动同步只有一份实现：触发、轮询、结束刷新都在这里。
        self.assertIn("'/api/settings/run'", hook)
        self.assertIn("`/api/settings/tasks/${taskId}`", hook)
        self.assertIn('emitRefresh()', hook)
        # 只轮询活动任务，卸载与新的运行都会让旧答案作废。
        self.assertIn('ACTIVE_STATUSES', hook)
        self.assertIn('generationRef', hook)
        self.assertIn('sync.task_conflict', hook)

    def test_reader_pages_trigger_sync_without_a_trip_to_the_console(self) -> None:
        articles = ARTICLES_PAGE.read_text(encoding='utf-8')
        batch = BATCH_ACTIONS.read_text(encoding='utf-8')
        groups = GROUPS_PAGE.read_text(encoding='utf-8')

        # 读者页面共用 hook，不再各自拼 /api/settings/run，也不跳管理台。
        for page in (articles, batch, groups):
            self.assertIn('useSyncRun', page)
            self.assertNotIn("'/api/settings/run'", page)
            self.assertNotIn("navigate('/admin/sync')", page)
        self.assertIn('articles.updateNow', articles)
        self.assertIn('runSync({ source: filters.source, groupId: filters.groupId })', articles)
        self.assertIn('runSync({ bizList: state.selectedAccounts })', batch)
        self.assertIn('runSync({ groupId })', groups)

    def test_console_buttons_run_through_the_admin_endpoint(self) -> None:
        for page in (ADMIN_SOURCE_PAGE, ADMIN_SYNC_SECTION, SYNC_SETTINGS_PANEL):
            source = page.read_text(encoding='utf-8')
            self.assertIn('useSyncRun', source)
            self.assertIn('siteWide: true', source)
            # 管理台按整站跑，不再占用读者那份只针对本人订阅的入口。
            self.assertNotIn("'/api/settings/run'", source)

        hook = SYNC_RUN_HOOK.read_text(encoding='utf-8')
        self.assertIn("'/api/settings/admin/run'", hook)
        # 只有共享 hook 会轮询任务，页面里那份手写的定时循环已经删掉。
        self.assertNotIn('maxAttempts', SYNC_SETTINGS_PANEL.read_text(encoding='utf-8'))

    def test_groups_page_syncs_selected_group_with_route_query(self) -> None:
        source = GROUPS_PAGE.read_text(encoding='utf-8')

        self.assertIn("useSearchParams", source)
        self.assertIn("searchParams.get('group')", source)
        self.assertIn("nextParams.set('group', nextGroup)", source)
        self.assertIn("dispatch({ type: 'SELECT_GROUP', groupId: nextGroupId })", source)


if __name__ == '__main__':
    unittest.main()
