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
  articleTimer: null,
};

let i18n = {};

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
      activateTab(tab);
    });
  });
};

const renderGroupList = () => {
  const list = $('#group-list');
  list.innerHTML = '';
  state.groups.forEach((group) => {
    const item = document.createElement('div');
    item.className = 'list-item' + (state.selectedGroupId === group.id ? ' is-active' : '');
    item.innerHTML = `
      <div>
        <div class="account-name">${group.name}</div>
        <div class="account-sub">${group.account_count || 0} ${t('groups.accounts', 'accounts')}</div>
      </div>
      <span class="badge">${group.account_count || 0}</span>
    `;
    item.addEventListener('click', () => {
      state.selectedGroupId = group.id;
      renderGroupList();
      renderGroupHeader();
      void loadAccounts();
    });
    list.appendChild(item);
  });
};

const renderGroupHeader = () => {
  const group = state.groups.find((item) => item.id === state.selectedGroupId);
  const nameEl = $('#group-current-name');
  const metaEl = $('#group-current-meta');
  const idEl = $('#group-current-id');
  const renameBtn = $('#btn-group-rename');
  const deleteBtn = $('#btn-group-delete');
  if (!group) {
    if (nameEl) {
      nameEl.textContent = t('groups.currentTitle', 'Group');
      nameEl.disabled = true;
    }
    if (idEl) {
      idEl.textContent = t('groups.currentIdEmpty', 'ID');
      idEl.dataset.id = '';
      idEl.disabled = true;
    }
    if (metaEl) metaEl.textContent = t('groups.currentEmpty', 'Select a group.');
    if (renameBtn) renameBtn.disabled = true;
    if (deleteBtn) deleteBtn.disabled = true;
    return;
  }
  if (nameEl) {
    nameEl.textContent = group.name;
    nameEl.title = group.name;
    nameEl.disabled = false;
  }
  if (idEl) {
    const idText = t('groups.currentId', 'ID: {id}').replace('{id}', group.id);
    idEl.textContent = idText;
    idEl.title = idText;
    idEl.dataset.id = String(group.id);
    idEl.disabled = false;
  }
  if (metaEl) {
    metaEl.textContent = t('groups.currentMeta', '{n} accounts').replace(
      '{n}',
      group.account_count || 0,
    );
  }
  if (renameBtn) renameBtn.disabled = false;
  if (deleteBtn) deleteBtn.disabled = false;
};

const loadGroups = async () => {
  const payload = await apiGet('/api/group');
  state.groups = payload.groups || [];
  state.defaultGroupId = payload.default_group_id;
  if (!state.selectedGroupId && state.defaultGroupId) {
    state.selectedGroupId = state.defaultGroupId;
  }
  renderGroupList();
  renderGroupHeader();
  renderGroupSelects();
  await loadAccounts();
  await loadAccountSearchResults();
};

const renderGroupSelects = () => {
  const selects = ['#article-group-filter', '#batch-group-select'];
  selects.forEach((selector) => {
    const select = $(selector);
    if (!select) return;
    select.innerHTML = '';
    if (selector !== '#batch-group-select') {
      const allOption = document.createElement('option');
      allOption.value = '';
      allOption.textContent = t('filters.allGroups', 'All Groups');
      select.appendChild(allOption);
    } else {
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = t('accounts.movePlaceholder', 'Move to group');
      select.appendChild(placeholder);
    }
    state.groups.forEach((group) => {
      const opt = document.createElement('option');
      opt.value = group.id;
      opt.textContent = group.name;
      select.appendChild(opt);
    });
  });
};

const renderAccounts = () => {
  const list = $('#account-list');
  list.innerHTML = '';
  state.accounts.forEach((account) => {
    const card = document.createElement('div');
    card.className = 'card';
    const checked = state.selectedAccounts.has(account.biz) ? 'checked' : '';
    const avatar = account.avatar_url || '';
    card.innerHTML = `
      <div class="card-header">
        <input class="account-check" type="checkbox" data-biz="${account.biz}" ${checked} />
      </div>
      <div class="account-meta">
        <img class="account-avatar" src="${avatar}" alt="" onerror="this.style.display='none'">
        <div>
          <div class="account-name">${account.nickname}</div>
          <div class="account-sub">${account.alias || account.biz}</div>
        </div>
      </div>
    `;
    const checkbox = card.querySelector('.account-check');
    checkbox.addEventListener('change', (event) => {
      const target = event.target;
      const biz = target.dataset.biz;
      if (target.checked) {
        state.selectedAccounts.add(biz);
      } else {
        state.selectedAccounts.delete(biz);
      }
      updateBatchCount();
      refreshSelectAll();
    });
    list.appendChild(card);
  });
  if (!state.accounts.length) {
    list.innerHTML = `<div class="empty-state">${t('accounts.empty', 'No accounts found.')}</div>`;
  }
  updateBatchCount();
  refreshSelectAll();
};

const renderAccountSearchResults = () => {
  const container = $('#account-search-results');
  if (!container) return;
  if (!state.searchResults.length) {
    container.innerHTML = '';
    container.classList.add('is-hidden');
    return;
  }
  container.classList.remove('is-hidden');
  container.innerHTML = '';
  state.searchResults.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'search-item';
    const avatar = item.avatar_url || '';
    row.innerHTML = `
      ${avatar ? `<img class="account-avatar" src="${avatar}" alt="" onerror="this.style.display='none'">` : '<div class="account-avatar"></div>'}
      <div class="search-meta">
        <div class="account-name">${item.nickname || ''}</div>
        <div class="account-sub">${item.alias || item.biz || ''}</div>
      </div>
      <div class="search-actions">
        ${
          item.is_added
            ? `<span class="search-tag">${t('accounts.searchAdded', 'Added')}</span>`
            : `<button class="btn ghost search-add" type="button" data-biz="${item.biz}">${t('accounts.searchAdd', 'Add')}</button>`
        }
      </div>
    `;
    const addBtn = row.querySelector('.search-add');
    if (addBtn) {
      addBtn.addEventListener('click', async () => {
        await apiSend('/api/account', 'POST', {
          biz: item.biz,
          nickname: item.nickname,
          alias: item.alias || null,
          round_head_img: item.round_head_img || null,
          group_id: state.selectedGroupId,
        });
        await loadGroups();
        await loadAccounts();
        await loadAccountSearchResults();
      });
    }
    container.appendChild(row);
  });
};

const loadAccountSearchResults = async () => {
  const searchInput = $('#account-search');
  const keyword = searchInput ? searchInput.value.trim() : '';
  if (!keyword) {
    state.searchResults = [];
    renderAccountSearchResults();
    return;
  }
  if (keyword.length < 2) {
    state.searchResults = [];
    renderAccountSearchResults();
    return;
  }
  const url = new URL('/api/account/search', window.location.origin);
  url.searchParams.set('q', keyword);
  url.searchParams.set('page_size', '8');
  try {
    const payload = await apiGet(url.pathname + url.search);
    state.searchResults = payload.results || [];
  } catch (err) {
    console.warn('Account search failed', err);
    state.searchResults = [];
  }
  renderAccountSearchResults();
};

const updateBatchCount = () => {
  const badge = $('#batch-count');
  const batchActions = $('#batch-actions');
  if (!badge) return;
  badge.textContent = state.selectedAccounts.size.toString();
  if (batchActions) {
    batchActions.classList.toggle('is-hidden', state.selectedAccounts.size === 0);
  }
};

const refreshSelectAll = () => {
  const selectAll = $('#account-select-all');
  if (!selectAll) return;
  const total = state.accounts.length;
  if (total === 0) {
    selectAll.checked = false;
    selectAll.indeterminate = false;
    return;
  }
  selectAll.checked = state.selectedAccounts.size === total;
  selectAll.indeterminate =
    state.selectedAccounts.size > 0 && state.selectedAccounts.size < total;
};

const loadAccounts = async () => {
  const groupId = state.selectedGroupId;
  const searchInput = $('#account-search');
  const search = searchInput ? searchInput.value.trim() : '';
  const url = new URL('/api/account', window.location.origin);
  if (groupId) url.searchParams.set('group_id', groupId);
  if (search) url.searchParams.set('q', search);
  url.searchParams.set('page_size', '100');
  const payload = await apiGet(url.pathname + url.search);
  state.accounts = payload.accounts || [];
  state.selectedAccounts = new Set();
  renderAccounts();
};

const renderArticleList = () => {
  const list = $('#article-list');
  list.innerHTML = '';
  state.articles.forEach((article) => {
    const card = document.createElement('div');
    card.className = 'article-card' + (state.selectedArticleId === article.id ? ' is-active' : '');
    const thumb = article.image_id ? `/api/image/${article.image_id}` : '';
    const avatar = article.account_avatar_url || '';
    const digest = article.digest || '';
    card.innerHTML = `
      ${thumb ? `<img class="article-thumb" src="${thumb}" alt="" onerror="this.style.display='none'">` : '<div class="article-thumb placeholder"></div>'}
      <div class="article-info">
        <div class="article-title">${article.title}</div>
        <div class="article-meta">
          ${avatar ? `<img class="article-avatar" src="${avatar}" alt="" onerror="this.style.display='none'">` : ''}
          <span>${article.account_nickname || ''}</span>
          <span>${formatDate(article.publish_at)}</span>
        </div>
        <div class="article-digest"></div>
      </div>
    `;
    const digestEl = card.querySelector('.article-digest');
    if (digestEl) {
      digestEl.textContent = digest;
      digestEl.title = digest;
    }
    card.addEventListener('click', () => selectArticle(article.id));
    list.appendChild(card);
  });
  if (!state.articles.length) {
    list.innerHTML = `<div class="empty-state">${t('articles.emptyList', 'No articles found.')}</div>`;
  }
};

const formatDate = (value) => {
  if (!value) return '';
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('zh-CN');
};

const loadArticles = async (reset = true) => {
  if (reset) {
    state.articlePage = 1;
    state.articles = [];
  }
  const groupId = $('#article-group-filter')?.value;
  const accountBiz = $('#article-account-filter')?.value;
  const search = $('#article-search')?.value.trim();
  const url = new URL('/api/article', window.location.origin);
  if (groupId) url.searchParams.set('group_id', groupId);
  if (accountBiz) url.searchParams.set('biz', accountBiz);
  if (search) url.searchParams.set('q', search);
  url.searchParams.set('content', '1');
  url.searchParams.set('page', state.articlePage.toString());
  url.searchParams.set('page_size', state.articlePageSize.toString());
  const payload = await apiGet(url.pathname + url.search);
  state.articles = state.articles.concat(payload.articles || []);
  renderArticleList();
};

const populateArticleAccountFilter = async () => {
  const groupId = $('#article-group-filter')?.value;
  const url = new URL('/api/account', window.location.origin);
  if (groupId) url.searchParams.set('group_id', groupId);
  url.searchParams.set('page_size', '200');
  const payload = await apiGet(url.pathname + url.search);
  const select = $('#article-account-filter');
  select.innerHTML = '';
  const allOption = document.createElement('option');
  allOption.value = '';
  allOption.textContent = t('filters.allAccounts', 'All Accounts');
  select.appendChild(allOption);
  payload.accounts.forEach((account) => {
    const opt = document.createElement('option');
    opt.value = account.biz;
    opt.textContent = account.nickname;
    select.appendChild(opt);
  });
};

const renderInline = (text) => {
  const escaped = String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
  return escaped
    .replace(/\\\|/g, '|')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br>');
};

const renderArticleContent = (payload) => {
  const container = $('#article-preview');
  container.innerHTML = '';
  const reader = document.createElement('div');
  reader.className = 'reader';
  if (!payload || !Array.isArray(payload.content)) {
    reader.innerHTML = `<div class="empty-state">${t('articles.empty', 'Select an article to preview.')}</div>`;
    container.appendChild(reader);
    return;
  }
  const fragment = document.createDocumentFragment();
  payload.content.forEach((block) => {
    if (!block || typeof block !== 'object') return;
    if (block.type === 'paragraph') {
      const p = document.createElement('p');
      p.innerHTML = renderInline(block.text || '');
      fragment.appendChild(p);
      return;
    }
    if (block.type === 'heading') {
      const level = Math.min(Math.max(Number(block.level) || 2, 2), 4);
      const h = document.createElement(`h${level}`);
      h.innerHTML = renderInline(block.text || '');
      fragment.appendChild(h);
      return;
    }
    if (block.type === 'image') {
      if (!block.image_id) {
        return;
      }
      const figure = document.createElement('figure');
      const img = document.createElement('img');
      if (block.image_id) {
        img.src = `/api/image/${block.image_id}`;
      }
      img.alt = block.alt || '';
      img.loading = 'lazy';
      figure.appendChild(img);
      fragment.appendChild(figure);
    }
  });
  reader.appendChild(fragment);
  container.appendChild(reader);
};

const selectArticle = async (id) => {
  state.selectedArticleId = id;
  renderArticleList();
  const payload = await apiGet(`/api/article/${id}`);
  renderArticleContent(payload);
};

const renderSyncHistory = () => {
  const list = $('#sync-history');
  if (!list) return;
  list.innerHTML = '';
  const history = state.syncStatus?.history || [];
  if (!history.length) {
    list.innerHTML = `<div class="empty-state">${t('sync.historyEmpty', 'No sync history.')}</div>`;
    return;
  }
  history.forEach((entry) => {
    const item = document.createElement('div');
    item.className = 'list-item';
    const status = entry.status || 'unknown';
    const saved = entry.saved || 0;
    const started = entry.started_at ? new Date(entry.started_at).toLocaleString('zh-CN') : '-';
    item.innerHTML = `
      <div>
        <div class="account-name">${t(`sync.status.${status}`, status)}</div>
        <div class="account-sub">${started}</div>
      </div>
      <span class="badge">+${saved}</span>
    `;
    list.appendChild(item);
  });
};

const renderSyncStatus = () => {
  const banner = $('#status-banner');
  const text = $('#banner-text');
  if (!banner || !text) return;
  if (!state.syncStatus) {
    banner.classList.add('is-hidden');
    return;
  }
  const status = state.syncStatus.status;
  if (status === 'login_required') {
    banner.classList.remove('is-hidden');
    text.textContent = t('sync.loginRequired', 'Login required. Please re-login.');
  } else if (status === 'failed') {
    banner.classList.remove('is-hidden');
    text.textContent = t('sync.failed', 'Sync failed. Please check login.');
  } else {
    banner.classList.add('is-hidden');
  }
  renderSyncHistory();
};

const loadSyncStatus = async () => {
  const payload = await apiGet('/api/sync');
  state.syncStatus = payload;
  renderSyncStatus();
};

const renderSyncSettings = (settings) => {
  if (!settings) return;
  $('#sync-enabled').checked = Boolean(settings.enabled);
  $('#sync-interval').value = settings.interval_minutes ?? 60;
  $('#sync-mode').value = settings.mode || 'incremental';
  $('#sync-recent-days').value = settings.recent_days ?? 7;
  $('#sync-page-size').value = settings.page_size ?? 10;
  $('#sync-page-limit').value = settings.page_limit ?? 2;
  $('#sync-sleep').value = settings.sleep_seconds ?? 0.05;
  $('#sync-content-limit').value = settings.content_limit ?? 20;
  $('#sync-skip-minutes').value = settings.skip_minutes ?? 30;
  $('#sync-download-content').checked = Boolean(settings.download_content);
  $('#sync-download-images').checked = Boolean(settings.download_images);
  $('#sync-alert-enabled').checked = Boolean(settings.alert_enabled);
  $('#sync-alert-email').value = settings.alert_email || '';
  const email = settings.email || {};
  $('#email-host').value = email.smtp_host || '';
  $('#email-port').value = email.smtp_port ?? 587;
  $('#email-user').value = email.smtp_user || '';
  $('#email-password').value = email.smtp_password || '';
  $('#email-from').value = email.from_email || '';
  $('#email-tls').checked = email.smtp_tls !== false;
};

const loadSyncSettings = async () => {
  const payload = await apiGet('/api/sync/settings');
  state.syncSettings = payload;
  renderSyncSettings(payload);
};

const saveSyncSettings = async () => {
  const body = {
    enabled: $('#sync-enabled').checked,
    interval_minutes: Number($('#sync-interval').value),
    mode: $('#sync-mode').value,
    recent_days: Number($('#sync-recent-days').value),
    page_size: Number($('#sync-page-size').value),
    page_limit: Number($('#sync-page-limit').value),
    sleep_seconds: Number($('#sync-sleep').value),
    content_limit: Number($('#sync-content-limit').value),
    skip_minutes: Number($('#sync-skip-minutes').value),
    download_content: $('#sync-download-content').checked,
    download_images: $('#sync-download-images').checked,
    alert_enabled: $('#sync-alert-enabled').checked,
    alert_email: $('#sync-alert-email').value.trim(),
    email: {
      smtp_host: $('#email-host').value.trim(),
      smtp_port: Number($('#email-port').value),
      smtp_user: $('#email-user').value.trim(),
      smtp_password: $('#email-password').value,
      smtp_tls: $('#email-tls').checked,
      from_email: $('#email-from').value.trim(),
    },
  };
  const payload = await apiSend('/api/sync/settings', 'PATCH', body);
  state.syncSettings = payload;
  renderSyncSettings(payload);
};

const triggerSyncRun = async () => {
  await apiSend('/api/sync/run', 'POST', {});
  setTimeout(loadSyncStatus, 1000);
};

const renderLoginStatus = (payload) => {
  state.loginStatus = payload;
  const status = payload.status || 'idle';
  const statusText = t(`login.status.${status}`, payload.message || status);
  $('#login-status').textContent = statusText;
  const qr = $('#login-qr');
  if (payload.qrcode_url) {
    qr.src = `${payload.qrcode_url}?ts=${Date.now()}`;
  } else {
    qr.removeAttribute('src');
  }
  const info = payload.last_login;
  if (info && info.nickname) {
    const relative = formatRelativeTime(info.updated_at);
    $('#login-meta').textContent = `${t('login.lastLogin', 'Last login')}: ${info.nickname} · ${relative}`;
  } else {
    $('#login-meta').textContent = '';
  }
  const topMeta = $('#last-login-info');
  if (topMeta) {
    if (info && info.updated_at) {
      const relative = formatRelativeTime(info.updated_at);
      topMeta.textContent = t('login.lastLoginAt', 'Last login {time}').replace('{time}', relative);
    } else {
      topMeta.textContent = '';
    }
  }
};

const formatRelativeTime = (isoString) => {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  const diff = Date.now() - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return t('time.justNow', 'just now');
  if (minutes < 60) return t('time.minutesAgo', '{n} minutes ago').replace('{n}', minutes);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t('time.hoursAgo', '{n} hours ago').replace('{n}', hours);
  const days = Math.floor(hours / 24);
  return t('time.daysAgo', '{n} days ago').replace('{n}', days);
};

const loadLoginStatus = async () => {
  const payload = await apiGet('/api/login');
  renderLoginStatus(payload);
  scheduleLoginPoll(payload.status);
};

const scheduleLoginPoll = (status) => {
  if (state.loginPollTimer) {
    clearInterval(state.loginPollTimer);
    state.loginPollTimer = null;
  }
  if (['waiting', 'scanned', 'refresh', 'starting'].includes(status)) {
    state.loginPollTimer = setInterval(async () => {
      try {
        const payload = await apiSend('/api/login/poll', 'POST', {});
        renderLoginStatus(payload);
        if (['success', 'error', 'idle'].includes(payload.status)) {
          clearInterval(state.loginPollTimer);
          state.loginPollTimer = null;
        }
      } catch (err) {
        console.warn('Login poll failed', err);
      }
    }, 2000);
  }
};

const startLogin = async () => {
  const payload = await apiSend('/api/login/start', 'POST', {});
  renderLoginStatus(payload);
  scheduleLoginPoll(payload.status);
};

const cancelLogin = async () => {
  const payload = await apiSend('/api/login/cancel', 'POST', {});
  renderLoginStatus(payload);
  scheduleLoginPoll(payload.status);
};

const initReaderControls = () => {
  const apply = () => {
    const root = document.documentElement;
    root.style.setProperty('--reader-width', `${$('#reader-width').value}px`);
    root.style.setProperty('--reader-font', `${$('#reader-font').value}px`);
    root.style.setProperty('--reader-line', $('#reader-line').value);
    root.style.setProperty('--reader-letter', `${$('#reader-letter').value}px`);
    const snapshot = {
      width: $('#reader-width').value,
      font: $('#reader-font').value,
      line: $('#reader-line').value,
      letter: $('#reader-letter').value,
    };
    localStorage.setItem('hippo-reader', JSON.stringify(snapshot));
  };

  const saved = localStorage.getItem('hippo-reader');
  if (saved) {
    try {
      const config = JSON.parse(saved);
      if (config.width) $('#reader-width').value = config.width;
      if (config.font) $('#reader-font').value = config.font;
      if (config.line) $('#reader-line').value = config.line;
      if (config.letter) $('#reader-letter').value = config.letter;
    } catch (err) {
      console.warn('Failed to parse reader settings', err);
    }
  }

  ['#reader-width', '#reader-font', '#reader-line', '#reader-letter'].forEach((selector) => {
    $(selector).addEventListener('input', apply);
  });
  apply();
};

const initArticleLayout = () => {
  const layout = $('.article-layout');
  const toggle = $('#btn-article-toggle');
  const updateLabel = () => {
    if (!layout || !toggle) return;
    const collapsed = layout.classList.contains('is-collapsed');
    const label = t(
      collapsed ? 'articles.showList' : 'articles.hideList',
      collapsed ? 'Show List' : 'Hide List',
    );
    toggle.setAttribute('title', label);
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('aria-expanded', String(!collapsed));
  };
  if (layout && toggle) {
    toggle.addEventListener('click', () => {
      layout.classList.toggle('is-collapsed');
      updateLabel();
    });
    updateLabel();
  }
  const resizer = $('#article-resizer');
  if (layout && resizer) {
    const root = document.documentElement;
    const saved = localStorage.getItem('hippo-article-width');
    if (saved) {
      root.style.setProperty('--article-list-width', `${saved}px`);
    }
    let startX = 0;
    let startWidth = 0;
    const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
    const onMove = (event) => {
      const clientX = event.touches ? event.touches[0].clientX : event.clientX;
      const delta = clientX - startX;
      const width = clamp(startWidth + delta, 220, 420);
      root.style.setProperty('--article-list-width', `${width}px`);
    };
    const onUp = (event) => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.removeEventListener('touchmove', onMove);
      document.removeEventListener('touchend', onUp);
      const current = getComputedStyle(root).getPropertyValue('--article-list-width').trim();
      if (current.endsWith('px')) {
        localStorage.setItem('hippo-article-width', current.replace('px', ''));
      }
    };
    resizer.addEventListener('mousedown', (event) => {
      startX = event.clientX;
      startWidth = parseFloat(
        getComputedStyle(root).getPropertyValue('--article-list-width'),
      );
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    resizer.addEventListener('touchstart', (event) => {
      startX = event.touches[0].clientX;
      startWidth = parseFloat(
        getComputedStyle(root).getPropertyValue('--article-list-width'),
      );
      document.addEventListener('touchmove', onMove);
      document.addEventListener('touchend', onUp);
    });
  }
  const readerToggle = $('#reader-toggle');
  const readerControls = $('#reader-controls');
  if (readerToggle && readerControls) {
    readerToggle.addEventListener('click', () => {
      readerControls.classList.toggle('is-open');
    });
  }
};

const bindEvents = () => {
  $('#btn-refresh').addEventListener('click', async () => {
    await loadGroups();
    await populateArticleAccountFilter();
    await loadArticles(true);
    await loadSyncStatus();
    await loadLoginStatus();
  });

  $('#btn-group-create').addEventListener('click', async () => {
    const name = prompt(t('groups.createPrompt', 'Group name'));
    if (!name) return;
    await apiSend('/api/group', 'POST', { name });
    await loadGroups();
  });
  $('#group-current-name').addEventListener('click', async () => {
    if (!state.selectedGroupId) return;
    activateTab('articles');
    const groupSelect = $('#article-group-filter');
    if (groupSelect) {
      groupSelect.value = String(state.selectedGroupId);
    }
    await populateArticleAccountFilter();
    await loadArticles(true);
  });
  $('#group-current-id').addEventListener('click', async () => {
    const id = $('#group-current-id').dataset.id;
    if (!id) return;
    try {
      await copyToClipboard(id);
    } catch (err) {
      console.warn('Failed to copy', err);
    }
  });
  $('#btn-group-rename').addEventListener('click', async () => {
    const group = state.groups.find((item) => item.id === state.selectedGroupId);
    if (!group) return;
    const name = prompt(t('groups.renamePrompt', 'New group name'), group.name);
    if (!name) return;
    await apiSend(`/api/group/${group.id}`, 'PATCH', { name });
    await loadGroups();
  });
  $('#btn-group-delete').addEventListener('click', async () => {
    const group = state.groups.find((item) => item.id === state.selectedGroupId);
    if (!group) return;
    const confirmed = confirm(t('groups.deleteConfirm', 'Delete this group?'));
    if (!confirmed) return;
    await apiSend(`/api/group/${group.id}`, 'DELETE');
    state.selectedGroupId = null;
    await loadGroups();
  });

  $('#btn-account-refresh').addEventListener('click', async () => {
    await loadAccounts();
    await loadAccountSearchResults();
  });
  $('#account-search').addEventListener('input', () => {
    clearTimeout(state.accountTimer);
    state.accountTimer = setTimeout(async () => {
      await loadAccounts();
      await loadAccountSearchResults();
    }, 300);
  });
  $('#account-select-all').addEventListener('change', (event) => {
    const checked = event.target.checked;
    state.selectedAccounts = new Set();
    if (checked) {
      state.accounts.forEach((account) => state.selectedAccounts.add(account.biz));
    }
    renderAccounts();
  });
  $('#btn-batch-move').addEventListener('click', async () => {
    const groupId = $('#batch-group-select').value;
    if (!groupId) {
      alert(t('accounts.moveSelectGroup', 'Select a target group.'));
      return;
    }
    if (!state.selectedAccounts.size) {
      alert(t('accounts.moveSelectAccounts', 'Select accounts to move.'));
      return;
    }
    await apiSend('/api/account/move', 'POST', {
      group_id: Number(groupId),
      biz_list: Array.from(state.selectedAccounts),
    });
    await loadAccounts();
  });

  $('#btn-article-refresh').addEventListener('click', () => loadArticles(true));
  $('#btn-article-more').addEventListener('click', () => {
    state.articlePage += 1;
    loadArticles(false);
  });
  $('#article-group-filter').addEventListener('change', async () => {
    await populateArticleAccountFilter();
    await loadArticles(true);
  });
  $('#article-account-filter').addEventListener('change', () => loadArticles(true));
  $('#article-search').addEventListener('input', () => {
    clearTimeout(state.articleTimer);
    state.articleTimer = setTimeout(() => loadArticles(true), 300);
  });

  $('#btn-login-start').addEventListener('click', startLogin);
  $('#btn-login-cancel').addEventListener('click', cancelLogin);
  $('#btn-login-refresh').addEventListener('click', startLogin);
  $('#btn-sync-save').addEventListener('click', saveSyncSettings);
  $('#btn-sync-run').addEventListener('click', triggerSyncRun);
  $('#btn-banner-login').addEventListener('click', async () => {
    activateTab('sync');
    await startLogin();
  });
};

const init = async () => {
  initTabs();
  initReaderControls();
  await loadI18n();
  initArticleLayout();
  await loadGroups();
  await populateArticleAccountFilter();
  await loadArticles(true);
  await loadSyncStatus();
  await loadSyncSettings();
  await loadLoginStatus();
  bindEvents();
};

window.addEventListener('DOMContentLoaded', init);
