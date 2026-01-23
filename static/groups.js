(() => {
  const {
    state,
    $,
    $$,
    apiGet,
    apiSend,
    t,
    copyToClipboard,
    showToast,
    activateTab,
  } = window.Hippo;

  const searchState = {
    page: 1,
    loading: false,
    hasMore: true,
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

  const showGroupContextMenu = (group, x, y) => {
    const menu = $('#group-context-menu');
    if (!menu) return;
    menu.dataset.groupId = String(group.id);
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    menu.classList.remove('is-hidden');
  };

  const hideGroupContextMenu = () => {
    const menu = $('#group-context-menu');
    if (!menu) return;
    menu.classList.add('is-hidden');
    menu.dataset.groupId = '';
  };

  const openAccountSearchModal = async () => {
    const modal = $('#account-search-modal');
    if (!modal) return;
    modal.classList.remove('is-hidden');
    const input = $('#account-search-input');
    if (input) {
      input.focus();
      input.select();
    }
    await loadAccountSearchResults();
  };

  const closeAccountSearchModal = () => {
    const modal = $('#account-search-modal');
    if (!modal) return;
    modal.classList.add('is-hidden');
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
      item.addEventListener('click', async () => {
        state.selectedGroupId = group.id;
        renderGroupList();
        renderGroupHeader();
        await loadAccounts();
      });
      item.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        showGroupContextMenu(group, event.clientX, event.clientY);
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
        idEl.disabled = true;
        idEl.dataset.id = '';
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
      idEl.disabled = false;
      idEl.dataset.id = String(group.id);
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
      const lastSynced = formatRelativeTime(account.last_synced_at);
      const lastUpdatedText = lastSynced
        ? t('accounts.lastUpdated', 'Last update {time}').replace('{time}', lastSynced)
        : t('accounts.lastUpdatedEmpty', 'No updates yet');
      const articleCount = Number(account.article_count || 0);
      const articleCountText = t('accounts.articleCount', '{n} articles').replace('{n}', articleCount);
      card.innerHTML = `
        <div class="card-header">
          <input class="account-check" type="checkbox" data-biz="${account.biz}" ${checked} />
        </div>
        <div class="account-meta">
          <img class="account-avatar" src="${avatar}" alt="" onerror="this.style.display='none'">
          <div>
            <div class="account-name">${account.nickname}</div>
            <div class="account-sub">${account.alias || account.biz}</div>
            <div class="account-sub account-stats">
              <span class="account-stat">${lastUpdatedText}</span>
              <span class="account-stat">${articleCountText}</span>
            </div>
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

  const renderAccountSearchResults = (items, append = false) => {
    const container = $('#account-search-results');
    if (!container) return;
    
    if (!append) {
      container.innerHTML = '';
      if (!items || !items.length) {
        container.classList.add('is-hidden');
        return;
      }
    } else if (!items || !items.length) {
      return;
    }

    container.classList.remove('is-hidden');
    
    items.forEach((item) => {
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
          
          // Optimistic update
          item.is_added = true;
          const parent = addBtn.parentElement;
          if (parent) {
            parent.innerHTML = `<span class="search-tag">${t('accounts.searchAdded', 'Added')}</span>`;
          }

          await loadGroups();
          await loadAccounts();
        });
      }
      container.appendChild(row);
    });
  };

  const loadAccountSearchResults = async (append = false) => {
    if (searchState.loading) return;
    
    const searchInput = $('#account-search-input');
    const keyword = searchInput ? searchInput.value.trim() : '';
    
    if (!keyword || keyword.length < 2) {
      state.searchResults = [];
      searchState.page = 1;
      searchState.hasMore = true;
      renderAccountSearchResults([]);
      return;
    }

    if (!append) {
      searchState.page = 1;
      searchState.hasMore = true;
      state.searchResults = [];
      // Scroll to top
      const container = $('#account-search-results');
      if (container) container.scrollTop = 0;
    }

    if (!searchState.hasMore) return;

    searchState.loading = true;
    const pageSize = 10;
    const url = new URL('/api/account/search', window.location.origin);
    url.searchParams.set('q', keyword);
    url.searchParams.set('page', searchState.page);
    url.searchParams.set('page_size', pageSize);
    
    try {
      const payload = await apiGet(url.pathname + url.search);
      const results = payload.results || [];
      
      if (results.length < pageSize) {
        searchState.hasMore = false;
      } else {
        searchState.page += 1;
      }

      if (append) {
        state.searchResults = [...state.searchResults, ...results];
      } else {
        state.searchResults = results;
      }
      
      renderAccountSearchResults(results, append);
    } catch (err) {
      console.warn('Account search failed', err);
      if (!append) {
        state.searchResults = [];
        renderAccountSearchResults([]);
      }
    } finally {
      searchState.loading = false;
    }
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

  const loadGroups = async () => {
    const payload = await apiGet('/api/group');
    state.groups = payload.groups || [];
    state.defaultGroupId = payload.default_group_id;
    if (state.selectedGroupId && !state.groups.some((group) => group.id === state.selectedGroupId)) {
      state.selectedGroupId = null;
    }
    if (!state.selectedGroupId && state.defaultGroupId) {
      state.selectedGroupId = state.defaultGroupId;
    }
    renderGroupList();
    renderGroupHeader();
    renderGroupSelects();
  };

  const bindEvents = () => {
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
      if (window.HippoArticles?.populateArticleAccountFilter) {
        await window.HippoArticles.populateArticleAccountFilter();
      }
      if (window.HippoArticles?.loadArticles) {
        await window.HippoArticles.loadArticles(true);
      }
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
    const groupRssBtn = $('#group-menu-rss');
    if (groupRssBtn) {
      groupRssBtn.addEventListener('click', async () => {
        const menu = $('#group-context-menu');
        const groupId = menu?.dataset.groupId;
        if (!groupId) return;
        const url = new URL('/api/feed/mixed', window.location.origin);
        url.searchParams.set('group_id', groupId);
        url.searchParams.set('limit', '50');
        url.searchParams.set('format', 'rss');
        try {
          await copyToClipboard(url.toString());
          showToast(t('groups.rssCopied', 'RSS address copied.'));
        } catch (err) {
          showToast(`${t('groups.rssPrompt', 'RSS address')}: ${url.toString()}`);
        }
        hideGroupContextMenu();
      });
    }

    $('#btn-account-refresh').addEventListener('click', async () => {
      await loadAccounts();
    });
    $('#account-search').addEventListener('input', () => {
      clearTimeout(state.accountTimer);
      state.accountTimer = setTimeout(async () => {
        await loadAccounts();
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

    $('#btn-account-add').addEventListener('click', openAccountSearchModal);
    $('#btn-account-search-close').addEventListener('click', closeAccountSearchModal);
    $('#account-search-modal').addEventListener('click', (event) => {
      if (event.target.id === 'account-search-modal') {
        closeAccountSearchModal();
      }
    });
    $('#account-search-input').addEventListener('input', () => {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(async () => {
        await loadAccountSearchResults();
      }, 300);
    });

    $('#account-search-results').addEventListener('scroll', (event) => {
      const el = event.target;
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 50) {
        if (!searchState.loading && searchState.hasMore) {
          loadAccountSearchResults(true);
        }
      }
    });

    document.addEventListener('click', (event) => {
      const menu = $('#group-context-menu');
      if (!menu || menu.classList.contains('is-hidden')) return;
      if (menu.contains(event.target)) return;
      hideGroupContextMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        hideGroupContextMenu();
      }
    });
    window.addEventListener(
      'scroll',
      () => {
        hideGroupContextMenu();
      },
      true,
    );
  };

  const refresh = async () => {
    await loadGroups();
    await loadAccounts();
  };

  const init = async () => {
    bindEvents();
    await refresh();
  };

  window.HippoGroups = {
    init,
    refresh,
    loadGroups,
    loadAccounts,
    renderGroupSelects,
  };
})();
