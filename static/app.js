const state = {
  groups: [],
  defaultGroupId: null,
  selectedGroupId: null,
  accounts: [],
  articles: [],
  articlePage: 1,
  articlePageSize: 20,
  selectedArticleId: null,
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

const initTabs = () => {
  $$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      $$('.tab').forEach((el) => el.classList.toggle('is-active', el === btn));
      $$('.view').forEach((view) => view.classList.toggle('is-active', view.id === `view-${tab}`));
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
      renderGroupDetail(group);
    });
    list.appendChild(item);
  });
};

const renderGroupDetail = async (group) => {
  const detail = $('#group-detail');
  if (!group) {
    detail.className = 'empty-state';
    detail.textContent = t('groups.empty', 'Select a group to view details.');
    return;
  }
  detail.className = '';
  const accounts = await apiGet(`/api/account?group_id=${group.id}&page_size=6`).catch(() => ({ accounts: [] }));
  const accountItems = accounts.accounts
    .map((account) => `<li>${account.nickname}</li>`)
    .join('');
  detail.innerHTML = `
    <div class="card">
      <div class="account-name">${group.name}</div>
      <div class="account-sub">${group.account_count || 0} ${t('groups.accounts', 'accounts')}</div>
      <div class="toolbar" style="margin-top:12px;">
        <button class="btn ghost" id="btn-group-rename">${t('groups.rename', 'Rename')}</button>
        <button class="btn ghost" id="btn-group-delete">${t('groups.delete', 'Delete')}</button>
      </div>
      <div style="margin-top:16px;">
        <div class="account-sub">${t('groups.accountsPreview', 'Accounts')}</div>
        <ul>${accountItems || `<li>${t('groups.none', 'No accounts')}</li>`}</ul>
      </div>
    </div>
  `;
  $('#btn-group-rename').addEventListener('click', async () => {
    const name = prompt(t('groups.renamePrompt', 'New group name'), group.name);
    if (!name) return;
    await apiSend(`/api/group/${group.id}`, 'PATCH', { name });
    await loadGroups();
  });
  $('#btn-group-delete').addEventListener('click', async () => {
    const confirmed = confirm(t('groups.deleteConfirm', 'Delete this group?'));
    if (!confirmed) return;
    await apiSend(`/api/group/${group.id}`, 'DELETE');
    state.selectedGroupId = null;
    await loadGroups();
    renderGroupDetail(null);
  });
};

const loadGroups = async () => {
  const payload = await apiGet('/api/group');
  state.groups = payload.groups || [];
  state.defaultGroupId = payload.default_group_id;
  if (!state.selectedGroupId && state.defaultGroupId) {
    state.selectedGroupId = state.defaultGroupId;
  }
  renderGroupList();
  renderGroupDetail(state.groups.find((g) => g.id === state.selectedGroupId));
  renderGroupSelects();
};

const renderGroupSelects = () => {
  const selects = ['#account-group-filter', '#article-group-filter'];
  selects.forEach((selector) => {
    const select = $(selector);
    if (!select) return;
    select.innerHTML = '';
    const allOption = document.createElement('option');
    allOption.value = '';
    allOption.textContent = t('filters.allGroups', 'All Groups');
    select.appendChild(allOption);
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
    card.innerHTML = `
      <div class="account-meta">
        <img class="account-avatar" src="${account.round_head_img || ''}" alt="" onerror="this.style.display='none'">
        <div>
          <div class="account-name">${account.nickname}</div>
          <div class="account-sub">${account.alias || account.biz}</div>
        </div>
      </div>
      <div class="account-sub">${account.group_name || ''}</div>
    `;
    list.appendChild(card);
  });
  if (!state.accounts.length) {
    list.innerHTML = `<div class="empty-state">${t('accounts.empty', 'No accounts found.')}</div>`;
  }
};

const loadAccounts = async () => {
  const groupId = $('#account-group-filter')?.value;
  const search = $('#account-search')?.value.trim();
  const url = new URL('/api/account', window.location.origin);
  if (groupId) url.searchParams.set('group_id', groupId);
  if (search) url.searchParams.set('q', search);
  url.searchParams.set('page_size', '100');
  const payload = await apiGet(url.pathname + url.search);
  state.accounts = payload.accounts || [];
  renderAccounts();
};

const renderArticleList = () => {
  const list = $('#article-list');
  list.innerHTML = '';
  state.articles.forEach((article) => {
    const card = document.createElement('div');
    card.className = 'article-card' + (state.selectedArticleId === article.id ? ' is-active' : '');
    const thumb = article.image_id ? `/api/image/${article.image_id}` : (article.cover || '');
    card.innerHTML = `
      <img class="article-thumb" src="${thumb}" alt="" onerror="this.style.display='none'">
      <div>
        <div class="article-title">${article.title}</div>
        <div class="article-meta">${article.account_nickname || ''}</div>
        <div class="article-meta">${formatDate(article.publish_at)}</div>
      </div>
    `;
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
      const figure = document.createElement('figure');
      const img = document.createElement('img');
      if (block.image_id) {
        img.src = `/api/image/${block.image_id}`;
      } else if (block.orig_url) {
        img.src = block.orig_url;
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

const bindEvents = () => {
  $('#btn-refresh').addEventListener('click', async () => {
    await loadGroups();
    await loadAccounts();
    await populateArticleAccountFilter();
    await loadArticles(true);
  });

  $('#btn-group-create').addEventListener('click', async () => {
    const name = prompt(t('groups.createPrompt', 'Group name'));
    if (!name) return;
    await apiSend('/api/group', 'POST', { name });
    await loadGroups();
  });

  $('#btn-account-refresh').addEventListener('click', loadAccounts);
  $('#account-group-filter').addEventListener('change', loadAccounts);
  $('#account-search').addEventListener('input', () => {
    clearTimeout(state.accountTimer);
    state.accountTimer = setTimeout(loadAccounts, 300);
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
};

const init = async () => {
  initTabs();
  initReaderControls();
  await loadI18n();
  await loadGroups();
  await loadAccounts();
  await populateArticleAccountFilter();
  await loadArticles(true);
  bindEvents();
};

window.addEventListener('DOMContentLoaded', init);
