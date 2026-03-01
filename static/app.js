const state = {
  groups: [],
  defaultGroupId: null,
  selectedGroupId: null,
  accounts: [],
  selectedAccounts: new Set(),
  searchResults: [],
  articles: [],
  articlePage: 1,
  articlePageSize: 20,
  selectedArticleId: null,
  syncStatus: null,
  syncSettings: null,
  syncTasks: null,
  syncPollTimer: null,
  loginStatus: null,
  loginPollTimer: null,
  accountTimer: null,
  searchTimer: null,
  articleTimer: null,
};

let i18n = {};
let toastTimer = null;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const handleAuthError = (message) => {
  const fallback = t('login.sessionExpired', 'Session expired. Please login again.');
  showToast(message || fallback);
  setHash('sync');
};

const apiGet = async (path) => {
  const res = await fetch(path, { headers: { "Accept": "application/json" } });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    if (res.status === 401) {
      handleAuthError(payload.error);
    }
    throw new Error(payload.error || `Request failed: ${res.status}`);
  }
  return res.json();
};

const apiSend = async (path, method, body, options = {}) => {
  const timeoutMs = Number(options.timeoutMs);
  const withTimeout = Number.isFinite(timeoutMs) && timeoutMs > 0;
  const controller = withTimeout ? new AbortController() : null;
  const timeoutId = withTimeout
    ? setTimeout(() => {
        controller.abort();
      }, Math.trunc(timeoutMs))
    : null;
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: controller?.signal,
    });
  } catch (err) {
    if (withTimeout && err?.name === 'AbortError') {
      const timeoutErr = new Error('Request timeout');
      timeoutErr.code = 'TIMEOUT';
      throw timeoutErr;
    }
    throw err;
  } finally {
    if (timeoutId) {
      clearTimeout(timeoutId);
    }
  }
  if (!res.ok && res.status !== 204) {
    const payload = await res.json().catch(() => ({}));
    if (res.status === 401) {
      handleAuthError(payload.error);
    }
    throw new Error(payload.error || `Request failed: ${res.status}`);
  }
  if (res.status === 204) {
    return {};
  }
  return res.json();
};

const t = (key, fallback) => i18n[key] || fallback || key;

const copyToClipboard = async (text) => {
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
};

const showToast = (message) => {
  const toast = $('#app-toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.remove('is-hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.add('is-hidden');
  }, 2400);
};

const applyI18n = () => {
  $$('[data-i18n]').forEach((node) => {
    const key = node.dataset.i18n;
    node.textContent = t(key, node.textContent);
  });
  $$('[data-i18n-placeholder]').forEach((node) => {
    const key = node.dataset.i18nPlaceholder;
    node.setAttribute('placeholder', t(key, node.getAttribute('placeholder')));
  });
};

const loadI18n = async () => {
  try {
    const res = await fetch('/i18n/zh-CN.json');
    if (res.ok) {
      i18n = await res.json();
    }
  } catch (err) {
    console.warn('Failed to load i18n', err);
  }
  applyI18n();
};

const availableTabs = () =>
  $$('.tab')
    .map((btn) => btn.dataset.tab)
    .filter(Boolean);

const TAB_MODULES = {
  groups: () => window.HippoGroups,
  articles: () => window.HippoArticles,
  sync: () => window.HippoSync,
};

const initializedTabs = new Set();
const tabInitPromises = new Map();
const routeEnterTimestamps = new Map();
let activeTab = null;
let routeToken = 0;
const ROUTE_ENTER_THROTTLE_MS = 500;

const normalizeTab = (tab) => {
  const options = availableTabs();
  if (!tab || options.length === 0) return null;
  return options.includes(tab) ? tab : null;
};

const getTabFromHash = () => {
  const hash = window.location.hash || '';
  const cleaned = hash.replace(/^#\/?/, '').trim();
  if (!cleaned) return null;
  const queryIndex = cleaned.indexOf('?');
  if (queryIndex === -1) {
    return cleaned;
  }
  return cleaned.slice(0, queryIndex).trim() || null;
};

const getCurrentTab = () => normalizeTab(getTabFromHash()) || 'groups';

const getTabModule = (tab) => {
  const resolver = TAB_MODULES[tab];
  return resolver ? resolver() : null;
};

const ensureTabInitialized = async (tab) => {
  if (initializedTabs.has(tab)) {
    return false;
  }
  const existing = tabInitPromises.get(tab);
  if (existing) {
    await existing;
    return false;
  }
  const module = getTabModule(tab);
  if (!module?.init) {
    initializedTabs.add(tab);
    return true;
  }
  const initPromise = (async () => {
    await module.init();
    initializedTabs.add(tab);
  })();
  tabInitPromises.set(tab, initPromise);
  try {
    await initPromise;
    return true;
  } finally {
    tabInitPromises.delete(tab);
  }
};

const runModuleHook = async (tab, hookName) => {
  const module = getTabModule(tab);
  const hook = module?.[hookName];
  if (typeof hook === 'function') {
    await hook();
  }
};

const shouldRunRouteEnter = (tab, previousTab) => {
  const now = Date.now();
  if (previousTab !== tab) {
    routeEnterTimestamps.set(tab, now);
    return true;
  }
  const last = routeEnterTimestamps.get(tab) || 0;
  if (now - last < ROUTE_ENTER_THROTTLE_MS) {
    return false;
  }
  routeEnterTimestamps.set(tab, now);
  return true;
};

const setHash = (tab, replace = false) => {
  const target = `#/${tab}`;
  if (replace) {
    history.replaceState(null, '', target);
    return;
  }
  if (window.location.hash !== target) {
    window.location.hash = target;
  }
};

const handleRoute = async (replace = false) => {
  const tab = getCurrentTab();
  activateTab(tab);
  if (getTabFromHash() !== tab) {
    setHash(tab, replace);
  }
  const previousTab = activeTab;
  activeTab = tab;
  if (previousTab && previousTab !== tab && initializedTabs.has(previousTab)) {
    await runModuleHook(previousTab, 'onRouteLeave');
  }
  const currentToken = ++routeToken;
  const initializedNow = await ensureTabInitialized(tab);
  if (currentToken !== routeToken) {
    return;
  }
  if (!initializedNow && shouldRunRouteEnter(tab, previousTab)) {
    await runModuleHook(tab, 'onRouteEnter');
  }
};

const activateTab = (tab) => {
  const target = $(`.tab[data-tab="${tab}"]`);
  if (!target) return;
  $$('.tab').forEach((el) => el.classList.toggle('is-active', el === target));
  $$('.view').forEach((view) =>
    view.classList.toggle('is-active', view.id === `view-${tab}`)
  );
};

const initTabs = () => {
  $$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      const normalized = normalizeTab(tab);
      if (!normalized) return;
      if (getCurrentTab() === normalized) {
        activateTab(normalized);
        void handleRoute(false);
        return;
      }
      setHash(normalized);
    });
  });
};

const loadViewFragments = async () => {
  const root = $('#view-root');
  if (!root) return;
  const pages = ['groups', 'articles', 'sync'];
  const html = await Promise.all(
    pages.map(async (name) => {
      const res = await fetch(`/pages/${name}.html`);
      if (!res.ok) {
        throw new Error(`Failed to load ${name} view`);
      }
      return res.text();
    })
  );
  root.innerHTML = html.join('\n');
};

const refreshCurrent = async () => {
  const tab = getCurrentTab();
  const initializedNow = await ensureTabInitialized(tab);
  if (initializedNow) {
    return;
  }
  const module = getTabModule(tab);
  if (module?.refresh) {
    await module.refresh();
  }
};

const bindGlobalEvents = () => {
  const refreshBtn = $('#btn-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      await refreshCurrent();
    });
  }
};

window.Hippo = {
  state,
  $,
  $$,
  apiGet,
  apiSend,
  t,
  copyToClipboard,
  showToast,
  applyI18n,
  loadI18n,
  activateTab,
};

const init = async () => {
  await loadViewFragments();
  initTabs();
  window.addEventListener('hashchange', () => {
    void handleRoute(false);
  });
  await loadI18n();
  await handleRoute(true);
  bindGlobalEvents();
};

window.addEventListener('DOMContentLoaded', init);
