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

const apiGet = async (path) => {
  const res = await fetch(path, { headers: { "Accept": "application/json" } });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}));
    throw new Error(payload.error || `Request failed: ${res.status}`);
  }
  return res.json();
};

const apiSend = async (path, method, body) => {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok && res.status !== 204) {
    const payload = await res.json().catch(() => ({}));
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

const normalizeTab = (tab) => {
  const options = availableTabs();
  if (!tab || options.length === 0) return null;
  return options.includes(tab) ? tab : null;
};

const getTabFromHash = () => {
  const hash = window.location.hash || '';
  const cleaned = hash.replace(/^#\/?/, '').trim();
  return cleaned || null;
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

const handleRoute = (replace = false) => {
  const tab = normalizeTab(getTabFromHash()) || 'groups';
  activateTab(tab);
  if (getTabFromHash() !== tab) {
    setHash(tab, replace);
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
      const targetHash = `#/${normalized}`;
      if (window.location.hash === targetHash) {
        activateTab(normalized);
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

const refreshAll = async () => {
  const tasks = [];
  if (window.HippoGroups?.refresh) {
    tasks.push(window.HippoGroups.refresh());
  }
  if (window.HippoArticles?.refresh) {
    tasks.push(window.HippoArticles.refresh());
  }
  if (window.HippoSync?.refresh) {
    tasks.push(window.HippoSync.refresh());
  }
  await Promise.all(tasks);
};

const bindGlobalEvents = () => {
  const refreshBtn = $('#btn-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      await refreshAll();
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
  handleRoute(true);
  window.addEventListener('hashchange', () => handleRoute(false));
  await loadI18n();
  if (window.HippoGroups?.init) {
    await window.HippoGroups.init();
  }
  if (window.HippoArticles?.init) {
    await window.HippoArticles.init();
  }
  if (window.HippoSync?.init) {
    await window.HippoSync.init();
  }
  bindGlobalEvents();
};

window.addEventListener('DOMContentLoaded', init);
