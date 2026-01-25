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

  const setAccountSearchLoading = (isLoading) => {
    const loading = $('#account-search-loading');
    if (!loading) return;
    loading.classList.toggle('is-hidden', !isLoading);
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

  const syncDefaults = {
    mode: 'incremental',
    recent_days: 7,
  };

  const ensureSyncSettings = async () => {
    if (state.syncSettings) return state.syncSettings;
    try {
      const payload = await apiGet('/api/sync/settings');
      state.syncSettings = payload;
      return payload;
    } catch (err) {
      console.warn('Failed to load sync settings', err);
      state.syncSettings = syncDefaults;
      return syncDefaults;
    }
  };

  const getSyncModeLabel = (mode) => {
    if (mode === 'incremental') return t('sync.modeIncremental', 'Incremental');
    if (mode === 'recent') return t('sync.modeRecent', 'Recent');
    if (mode === 'full') return t('sync.modeFull', 'Full');
    return mode || '';
  };

  const getDefaultRecentDays = () => {
    return state.syncSettings?.recent_days ?? syncDefaults.recent_days;
  };

  const buildSyncModeOptions = (select, { includeInherit, inheritLabel, selected }) => {
    if (!select) return;
    select.innerHTML = '';
    if (includeInherit) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = inheritLabel;
      select.appendChild(opt);
    }
    const options = [
      { value: 'incremental', label: t('sync.modeIncremental', 'Incremental') },
      { value: 'recent', label: t('sync.modeRecent', 'Recent') },
      { value: 'full', label: t('sync.modeFull', 'Full') },
    ];
    options.forEach((item) => {
      const opt = document.createElement('option');
      opt.value = item.value;
      opt.textContent = item.label;
      select.appendChild(opt);
    });
    if (selected !== undefined && selected !== null) {
      select.value = selected;
    }
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

  const triggerGroupSync = async (groupId) => {
    try {
      await apiSend('/api/sync/run', 'POST', { group_id: groupId });
      showToast(t('groups.syncTriggered', 'Group sync triggered.'));
    } catch (err) {
      console.warn('Failed to trigger group sync', err);
      showToast(t('groups.syncTriggerFailed', 'Failed to trigger group sync.'));
    }
  };

  const resolveSelectedGroupId = () => {
    if (state.selectedGroupId) {
      return state.selectedGroupId;
    }
    const firstBiz = state.selectedAccounts.values().next().value;
    if (!firstBiz) return null;
    const account = state.accounts.find((item) => item.biz === firstBiz);
    return account?.group_id || null;
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
      item.className =
        'list-item group-list-item' + (state.selectedGroupId === group.id ? ' is-active' : '');
      item.innerHTML = `
        <div>
          <div class="account-name">${group.name}</div>
          <div class="account-sub">${group.account_count || 0} ${t('groups.accounts', 'accounts')}</div>
        </div>
        <div class="group-item-actions">
          <button class="group-sync-btn" type="button" title="${t('groups.syncNow', 'Sync')}" aria-label="${t('groups.syncNow', 'Sync')}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 6V3L8 7l4 4V8c2.2 0 4 1.8 4 4a4 4 0 0 1-4 4 4 4 0 0 1-3.8-2.6l-1.9.6A6 6 0 0 0 12 20a6 6 0 0 0 0-12z"/></svg>
          </button>
          <span class="badge">${group.account_count || 0}</span>
        </div>
      `;
      item.addEventListener('click', async () => {
        state.selectedGroupId = group.id;
        renderGroupList();
        renderGroupHeader();
        renderGroupSelects();
        await loadAccounts();
      });
      item.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        showGroupContextMenu(group, event.clientX, event.clientY);
      });
      const syncBtn = item.querySelector('.group-sync-btn');
      if (syncBtn) {
        syncBtn.addEventListener('click', async (event) => {
          event.stopPropagation();
          await triggerGroupSync(group.id);
        });
      }
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
    const syncModeSelect = $('#group-sync-mode');
    const syncDaysInput = $('#group-sync-days');
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
      if (syncModeSelect) {
        syncModeSelect.innerHTML = '';
        syncModeSelect.disabled = true;
      }
      if (syncDaysInput) {
        syncDaysInput.value = '';
        syncDaysInput.disabled = true;
      }
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
    if (syncModeSelect && syncDaysInput) {
      const defaultMode = state.syncSettings?.mode || syncDefaults.mode;
      const inheritLabel = t('groups.syncModeInherit', 'Follow global ({mode})').replace(
        '{mode}',
        getSyncModeLabel(defaultMode),
      );
      buildSyncModeOptions(syncModeSelect, {
        includeInherit: true,
        inheritLabel,
        selected: group.sync_mode || '',
      });
      const defaultRecentDays = getDefaultRecentDays();
      const groupRecent = group.sync_recent_days ?? defaultRecentDays;
      syncDaysInput.value = String(groupRecent);
      syncDaysInput.disabled = group.sync_mode !== 'recent';
      syncModeSelect.disabled = false;
    }
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

    const batchSyncMode = $('#batch-sync-mode');
    const batchSyncDays = $('#batch-sync-days');
    if (batchSyncMode) {
      const activeGroup = state.groups.find((item) => item.id === state.selectedGroupId);
      const groupMode = activeGroup?.sync_mode || '';
      const defaultMode = groupMode || state.syncSettings?.mode || syncDefaults.mode;
      const inheritLabel = groupMode
        ? t('accounts.syncModeInheritGroup', 'Follow group ({mode})').replace(
            '{mode}',
            getSyncModeLabel(defaultMode),
          )
        : t('accounts.syncModeInherit', 'Follow global ({mode})').replace(
            '{mode}',
            getSyncModeLabel(defaultMode),
          );
      buildSyncModeOptions(batchSyncMode, {
        includeInherit: true,
        inheritLabel,
        selected: batchSyncMode.value || '',
      });
    }
    if (batchSyncDays) {
      batchSyncDays.value = String(getDefaultRecentDays());
      batchSyncDays.disabled = batchSyncMode ? batchSyncMode.value !== 'recent' : true;
    }
  };

  const renderAccounts = () => {
    const list = $('#account-list');
    list.innerHTML = '';
    const activeGroup = state.groups.find((item) => item.id === state.selectedGroupId);
    state.accounts.forEach((account) => {
      const card = document.createElement('div');
      card.className = `card${account.is_disabled ? ' is-disabled' : ''}`;
      const checked = state.selectedAccounts.has(account.biz) ? 'checked' : '';
      const avatar = account.avatar_url || '';
      const lastSynced = formatRelativeTime(account.last_synced_at);
      const lastUpdatedText = lastSynced
        ? t('accounts.lastUpdated', 'Last update {time}').replace('{time}', lastSynced)
        : t('accounts.lastUpdatedEmpty', 'No updates yet');
      const articleCount = Number(account.article_count || 0);
      const articleCountText = t('accounts.articleCount', '{n} articles').replace('{n}', articleCount);
      const storedMode = account.sync_mode || '';
      const groupMode = activeGroup?.sync_mode || '';
      const defaultMode = groupMode || state.syncSettings?.mode || syncDefaults.mode;
      const inheritLabel = groupMode
        ? t('accounts.syncModeInheritGroup', 'Follow group ({mode})').replace(
            '{mode}',
            getSyncModeLabel(defaultMode),
          )
        : t('accounts.syncModeInherit', 'Follow global ({mode})').replace(
            '{mode}',
            getSyncModeLabel(defaultMode),
          );
      const defaultRecentDays = getDefaultRecentDays();
      const groupRecentDays = activeGroup?.sync_recent_days;
      const baseRecentDays = groupRecentDays ?? defaultRecentDays;
      const initialRecentDays = account.sync_recent_days ?? baseRecentDays;
      const isRecentEditable = storedMode === 'recent';
      const syncModeLabel = t('accounts.syncMode', 'Update strategy');
      const syncRecentLabel = t('accounts.syncRecentDays', 'Recent days');
      const disabledTag = account.is_disabled
        ? `<span class="account-tag">${t('accounts.syncDisabled', 'Sync disabled')}</span>`
        : '';
      const toggleTitle = account.is_disabled
        ? t('accounts.enableSync', 'Enable sync')
        : t('accounts.disableSync', 'Disable sync');
      const toggleIcon = account.is_disabled
        ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>'
        : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>';
      card.innerHTML = `
        <div class="card-header">
          <input class="account-check" type="checkbox" data-biz="${account.biz}" ${checked} />
          <button class="account-toggle" type="button" data-biz="${account.biz}" title="${toggleTitle}" aria-label="${toggleTitle}">
            ${toggleIcon}
          </button>
        </div>
        <div class="account-meta">
          <img class="account-avatar" src="${avatar}" alt="" onerror="this.style.display='none'">
          <div>
            <div class="account-name">${account.nickname}</div>
            <div class="account-sub">${account.alias || account.biz}</div>
            <div class="account-sub account-stats">
              <span class="account-stat">${lastUpdatedText}</span>
              <span class="account-stat">${articleCountText}</span>
              ${disabledTag}
            </div>
          </div>
        </div>
        <div class="account-sync">
          <div class="account-sync-row">
            <span class="account-sync-label">${syncModeLabel}</span>
            <select class="account-sync-mode" data-biz="${account.biz}">
              <option value="" ${storedMode === '' ? 'selected' : ''}>${inheritLabel}</option>
              <option value="incremental" ${storedMode === 'incremental' ? 'selected' : ''}>
                ${t('sync.modeIncremental', 'Incremental')}
              </option>
              <option value="recent" ${storedMode === 'recent' ? 'selected' : ''}>
                ${t('sync.modeRecent', 'Recent')}
              </option>
              <option value="full" ${storedMode === 'full' ? 'selected' : ''}>
                ${t('sync.modeFull', 'Full')}
              </option>
            </select>
          </div>
          <div class="account-sync-row">
            <span class="account-sync-label">${syncRecentLabel}</span>
            <input
              class="account-sync-days"
              type="number"
              min="1"
              value="${initialRecentDays}"
              ${isRecentEditable ? '' : 'disabled'}
            />
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
      const modeSelect = card.querySelector('.account-sync-mode');
      const daysInput = card.querySelector('.account-sync-days');
      const toggleBtn = card.querySelector('.account-toggle');
      const resolveRecentDays = (value) => {
        const parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed) || parsed < 1) {
          return baseRecentDays;
        }
        return parsed;
      };
      const updateDaysState = (mode) => {
        const editable = mode === 'recent';
        daysInput.disabled = !editable;
      };
      const saveSyncSettings = async (nextMode, nextDays) => {
        const previousMode = account.sync_mode || '';
        const previousDays = account.sync_recent_days;
        const payload = {};
        payload.sync_mode = nextMode ? nextMode : null;
        if (nextMode === 'recent') {
          payload.sync_recent_days = nextDays;
        }
        try {
          await apiSend(`/api/account/${account.biz}`, 'PATCH', payload);
          account.sync_mode = payload.sync_mode || null;
          if ('sync_recent_days' in payload) {
            account.sync_recent_days = payload.sync_recent_days;
          }
          showToast(t('accounts.syncSaved', 'Sync strategy updated.'));
        } catch (err) {
          console.warn('Failed to update sync settings', err);
          account.sync_mode = previousMode || null;
          account.sync_recent_days = previousDays;
          modeSelect.value = previousMode;
          const fallbackDays = previousDays ?? baseRecentDays;
          daysInput.value = String(fallbackDays);
          updateDaysState(previousMode);
          showToast(t('accounts.syncFailed', 'Failed to update sync strategy.'));
        }
      };
      modeSelect.addEventListener('change', async (event) => {
        const nextMode = event.target.value;
        updateDaysState(nextMode);
        if (nextMode === 'recent') {
          const normalized = resolveRecentDays(daysInput.value);
          daysInput.value = String(normalized);
          await saveSyncSettings(nextMode, normalized);
        } else {
          await saveSyncSettings(nextMode, undefined);
        }
      });
      daysInput.addEventListener('change', async () => {
        if (modeSelect.value !== 'recent') return;
        const normalized = resolveRecentDays(daysInput.value);
        daysInput.value = String(normalized);
        await saveSyncSettings('recent', normalized);
      });
      daysInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          daysInput.blur();
        }
      });
      if (toggleBtn) {
        toggleBtn.addEventListener('click', async () => {
          const nextDisabled = !account.is_disabled;
          try {
            await apiSend(`/api/account/${account.biz}`, 'PATCH', { is_disabled: nextDisabled });
            account.is_disabled = nextDisabled;
            renderAccounts();
            showToast(
              nextDisabled
                ? t('accounts.syncDisabledToast', 'Sync disabled.')
                : t('accounts.syncEnabledToast', 'Sync enabled.'),
            );
          } catch (err) {
            console.warn('Failed to update sync status', err);
            showToast(t('accounts.syncToggleFailed', 'Failed to update sync status.'));
          }
        });
      }
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
      setAccountSearchLoading(false);
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
    setAccountSearchLoading(true);
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
      setAccountSearchLoading(false);
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
    await ensureSyncSettings();
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
    $('#group-sync-mode').addEventListener('change', async (event) => {
      const group = state.groups.find((item) => item.id === state.selectedGroupId);
      if (!group) return;
      const mode = event.target.value;
      const daysInput = $('#group-sync-days');
      const defaultRecentDays = getDefaultRecentDays();
      const resolveDays = (value) => {
        const parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed) || parsed < 1) {
          return defaultRecentDays;
        }
        return parsed;
      };
      const nextDays = resolveDays(daysInput.value);
      daysInput.value = String(nextDays);
      daysInput.disabled = mode !== 'recent';
      try {
        const payload = {
          sync_mode: mode || null,
        };
        if (mode === 'recent') {
          payload.sync_recent_days = nextDays;
        }
        const updated = await apiSend(`/api/group/${group.id}`, 'PATCH', payload);
        group.sync_mode = updated.sync_mode || null;
        group.sync_recent_days = updated.sync_recent_days ?? group.sync_recent_days;
        renderGroupHeader();
        renderAccounts();
        renderGroupSelects();
        showToast(t('groups.syncSaved', 'Default sync updated.'));
      } catch (err) {
        console.warn('Failed to update group sync', err);
        renderGroupHeader();
        showToast(t('groups.syncFailed', 'Failed to update default sync.'));
      }
    });
    $('#group-sync-days').addEventListener('change', async () => {
      const group = state.groups.find((item) => item.id === state.selectedGroupId);
      if (!group) return;
      if ($('#group-sync-mode').value !== 'recent') return;
      const daysInput = $('#group-sync-days');
      const defaultRecentDays = getDefaultRecentDays();
      const parsed = Number.parseInt(daysInput.value, 10);
      const nextDays = Number.isFinite(parsed) && parsed > 0 ? parsed : defaultRecentDays;
      daysInput.value = String(nextDays);
      try {
        const updated = await apiSend(`/api/group/${group.id}`, 'PATCH', {
          sync_recent_days: nextDays,
        });
        group.sync_recent_days = updated.sync_recent_days ?? group.sync_recent_days;
        renderAccounts();
        renderGroupSelects();
        showToast(t('groups.syncSaved', 'Default sync updated.'));
      } catch (err) {
        console.warn('Failed to update group sync days', err);
        renderGroupHeader();
        showToast(t('groups.syncFailed', 'Failed to update default sync.'));
      }
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
    $('#batch-sync-mode').addEventListener('change', () => {
      const mode = $('#batch-sync-mode').value;
      const daysInput = $('#batch-sync-days');
      if (!daysInput) return;
      const defaultRecentDays = getDefaultRecentDays();
      if (mode === 'recent') {
        const parsed = Number.parseInt(daysInput.value, 10);
        const nextDays = Number.isFinite(parsed) && parsed > 0 ? parsed : defaultRecentDays;
        daysInput.value = String(nextDays);
        daysInput.disabled = false;
      } else {
        daysInput.disabled = true;
      }
    });
    $('#btn-batch-sync').addEventListener('click', async () => {
      const mode = $('#batch-sync-mode').value;
      const daysInput = $('#batch-sync-days');
      if (!state.selectedAccounts.size) {
        alert(t('accounts.moveSelectAccounts', 'Select accounts to move.'));
        return;
      }
      const payload = {
        biz_list: Array.from(state.selectedAccounts),
        sync_mode: mode || null,
      };
      if (mode === 'recent') {
        const defaultRecentDays = getDefaultRecentDays();
        const parsed = Number.parseInt(daysInput.value, 10);
        const nextDays = Number.isFinite(parsed) && parsed > 0 ? parsed : defaultRecentDays;
        daysInput.value = String(nextDays);
        payload.sync_recent_days = nextDays;
      }
      try {
        await apiSend('/api/account/batch', 'POST', payload);
        showToast(t('accounts.syncSaved', 'Sync strategy updated.'));
        await loadAccounts();
      } catch (err) {
        console.warn('Batch sync update failed', err);
        showToast(t('accounts.syncFailed', 'Failed to update sync strategy.'));
      }
    });
    $('#btn-batch-group-sync').addEventListener('click', async () => {
      if (!state.selectedAccounts.size) {
        alert(t('accounts.moveSelectAccounts', 'Select accounts to move.'));
        return;
      }
      const groupId = resolveSelectedGroupId();
      if (!groupId) {
        showToast(t('groups.syncTriggerFailed', 'Failed to trigger group sync.'));
        return;
      }
      await triggerGroupSync(groupId);
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
    await ensureSyncSettings();
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
