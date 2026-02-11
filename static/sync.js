(() => {
  const { state, $, apiGet, apiSend, t, activateTab } = window.Hippo;

  const HISTORY_PAGE_SIZE = 5;

  const renderSyncHistory = () => {
    const list = $('#sync-history');
    if (!list) return;
    list.innerHTML = '';
    const history = state.syncStatus?.history || [];
    const tasks = state.syncTasks || [];
    const activeTask =
      tasks.find((task) => task.status === 'running') ||
      tasks.find((task) => task.status === 'pending');
    if (!history.length && !activeTask) {
      list.innerHTML = `<div class="empty-state">${t('sync.historyEmpty', 'No sync history.')}</div>`;
      return;
    }
    if (activeTask) {
      const item = document.createElement('div');
      item.className = 'list-item is-running';
      const isPending = activeTask.status === 'pending';
      const currentAccount = activeTask.current_account || {};
      const accountName =
        currentAccount.nickname ||
        currentAccount.biz ||
        (isPending
          ? t('sync.pendingUnknown', 'Waiting for current sync to finish')
          : t('sync.runningUnknown', 'Unknown account'));
      const startedAt = activeTask.started_at || activeTask.created_at;
      const minutes = startedAt
        ? Math.max(1, Math.floor((Date.now() - new Date(startedAt).getTime()) / 60000))
        : 0;
      const progressBadge =
        activeTask.accounts_total > 0
          ? `${activeTask.accounts_done || 0}/${activeTask.accounts_total}`
          : '';
      const phase =
        isPending
          ? t('sync.pendingHint', 'Waiting for running sync slot')
          : activeTask.last_log === 'downloading_images' || (activeTask.last_log && activeTask.last_log.includes('图片'))
          ? t('sync.runningImages', 'Downloading images')
          : activeTask.current_article?.title
            ? t('sync.runningArticle', 'Processing {title}').replace(
                '{title}',
                activeTask.current_article.title
              )
            : null;
      item.innerHTML = `
        <div>
          <div class="account-name">${t(isPending ? 'sync.pendingTitle' : 'sync.runningTitle', isPending ? 'Queued {name}' : 'Syncing {name}').replace('{name}', accountName)}</div>
          <div class="account-sub">${t(isPending ? 'sync.pendingSince' : 'sync.runningSince', isPending ? 'Queued for {n} minutes' : 'Started {n} minutes ago').replace('{n}', minutes)}</div>
          ${phase ? `<div class="account-sub">${phase}</div>` : ''}
        </div>
        ${progressBadge ? `<span class="badge">${progressBadge}</span>` : ''}
      `;
      list.appendChild(item);
    }
    const page = state.historyPage || 1;
    const start = (page - 1) * HISTORY_PAGE_SIZE;
    const paged = history.slice(start, start + HISTORY_PAGE_SIZE);
    paged.forEach((entry) => {
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

    const totalPages = Math.max(1, Math.ceil(history.length / HISTORY_PAGE_SIZE));
    const prevBtn = $('#btn-history-prev');
    const nextBtn = $('#btn-history-next');
    if (prevBtn && nextBtn) {
      prevBtn.disabled = page <= 1;
      nextBtn.disabled = page >= totalPages;
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

  const formatRelativeHours = (isoString) => {
    if (!isoString) return '';
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) return '';
    const diff = Date.now() - date.getTime();
    const minutes = Math.floor(diff / 60000);
    if (minutes < 1) return t('time.justNow', 'just now');
    if (minutes < 60) return t('time.minutesAgo', '{n} minutes ago').replace('{n}', minutes);
    const hours = Math.floor(minutes / 60);
    return t('time.hoursAgo', '{n} hours ago').replace('{n}', hours);
  };

  const loginActionStatuses = ['starting', 'waiting', 'scanned', 'refresh'];

  const updateLoginActions = (status) => {
    const startBtn = $('#btn-login-start');
    const cancelBtn = $('#btn-login-cancel');
    if (!startBtn || !cancelBtn) return;
    const inProgress = loginActionStatuses.includes(status);
    startBtn.textContent = inProgress
      ? t('login.refresh', 'Refresh Login')
      : t('login.start', 'Start Login');
    cancelBtn.classList.toggle('is-hidden', !inProgress);
  };

  const renderSyncStatus = () => {
    const banner = $('#status-banner');
    const text = $('#banner-text');
    const syncMeta = $('#last-sync-info');
    if (!banner || !text) return;
    if (!state.syncStatus) {
      banner.classList.add('is-hidden');
      if (syncMeta) syncMeta.textContent = '';
      return;
    }
    if (syncMeta) {
      const finished = state.syncStatus.last_finished_at;
      if (finished) {
        const relative = formatRelativeTime(finished);
        syncMeta.textContent = t('sync.lastSyncAt', 'Last sync {time}').replace('{time}', relative);
      } else {
        syncMeta.textContent = '';
      }
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
    state.historyPage = 1;
    renderSyncStatus();
  };

  const loadSyncTasks = async () => {
    const payload = await apiGet('/api/sync/tasks?limit=5');
    state.syncTasks = payload.tasks || [];
    state.historyPage = state.historyPage || 1;
    renderSyncHistory();
  };

  const renderSyncSettings = (settings) => {
    if (!settings) return;
    $('#sync-enabled').checked = Boolean(settings.enabled);
    $('#sync-interval').value = settings.interval_minutes ?? 60;
    $('#sync-sleep').value = settings.sleep_seconds ?? 0.05;
    $('#sync-skip-minutes').value = settings.skip_minutes ?? 30;
    $('#sync-download-content').checked = Boolean(settings.download_content);
    $('#sync-download-images').checked = Boolean(settings.download_images);
    $('#sync-alert-enabled').checked = Boolean(settings.alert_enabled);
    $('#sync-alert-email').value = settings.alert_email || '';
    const email = settings.email || {};
    $('#email-smtp-host').value = email.smtp_host || '';
    $('#email-smtp-user').value = email.smtp_user || '';
    $('#email-smtp-password').value = email.smtp_password || '';
    $('#email-from').value = email.from_email || '';
    const tlsEnabled = email.smtp_tls !== false;
    $('#email-tls').checked = tlsEnabled;
    $('#email-smtp-port').value = email.smtp_port ?? (tlsEnabled ? 587 : 25);
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
      sleep_seconds: Number($('#sync-sleep').value),
      skip_minutes: Number($('#sync-skip-minutes').value),
      download_content: $('#sync-download-content').checked,
      download_images: $('#sync-download-images').checked,
      alert_enabled: $('#sync-alert-enabled').checked,
      alert_email: $('#sync-alert-email').value.trim(),
      email: {
        smtp_host: $('#email-smtp-host').value.trim(),
        smtp_port: Number($('#email-smtp-port').value),
        smtp_user: $('#email-smtp-user').value.trim(),
        smtp_password: $('#email-smtp-password').value,
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
    setTimeout(async () => {
      await loadSyncTasks();
      await loadSyncStatus();
    }, 1000);
  };

  const renderLoginStatus = (payload) => {
    state.loginStatus = payload;
    const status = payload.status || 'idle';
    const statusText = t(`login.status.${status}`, payload.message || status);
    $('#login-status').textContent = statusText;
    const qr = $('#login-qr');
    updateLoginActions(status);
    if (payload.qrcode_url) {
      qr.src = `${payload.qrcode_url}?ts=${Date.now()}`;
      qr.classList.remove('is-hidden');
    } else {
      qr.removeAttribute('src');
      qr.classList.add('is-hidden');
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
        const relative = formatRelativeHours(info.updated_at);
        topMeta.textContent = t('login.lastLoginAt', 'Last login {time}').replace('{time}', relative);
      } else {
        topMeta.textContent = '';
      }
    }
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

  const bindEvents = () => {
    $('#btn-login-start').addEventListener('click', startLogin);
    $('#btn-login-cancel').addEventListener('click', cancelLogin);
    $('#btn-sync-save').addEventListener('click', saveSyncSettings);
    $('#btn-sync-run').addEventListener('click', triggerSyncRun);
    const prev = $('#btn-history-prev');
    const next = $('#btn-history-next');
    if (prev && next) {
      prev.addEventListener('click', () => {
        state.historyPage = Math.max(1, (state.historyPage || 1) - 1);
        renderSyncHistory();
      });
      next.addEventListener('click', () => {
        const history = state.syncStatus?.history || [];
        const totalPages = Math.max(1, Math.ceil(history.length / HISTORY_PAGE_SIZE));
        state.historyPage = Math.min(totalPages, (state.historyPage || 1) + 1);
        renderSyncHistory();
      });
    }
    const emailTls = $('#email-tls');
    if (emailTls) {
      emailTls.addEventListener('change', () => {
        const port = $('#email-smtp-port');
        if (!port) return;
        port.value = emailTls.checked ? '587' : '25';
      });
    }
    $('#btn-banner-login').addEventListener('click', async () => {
      activateTab('sync');
      await startLogin();
    });
  };

  const isSyncActive = () => $('#view-sync')?.classList.contains('is-active');

  const startSyncPoll = () => {
    if (state.syncPollTimer) {
      clearInterval(state.syncPollTimer);
    }
    state.syncPollTimer = setInterval(async () => {
      if (!isSyncActive()) return;
      try {
        await loadSyncTasks();
        await loadSyncStatus();
      } catch (err) {
        console.warn('Sync poll failed', err);
      }
    }, 1000);
  };

  const stopSyncPoll = () => {
    if (!state.syncPollTimer) return;
    clearInterval(state.syncPollTimer);
    state.syncPollTimer = null;
  };

  const refresh = async () => {
    await loadSyncTasks();
    await loadSyncStatus();
    await loadSyncSettings();
    await loadLoginStatus();
    startSyncPoll();
  };

  const init = async () => {
    bindEvents();
    await refresh();
  };

  const onRouteEnter = async () => {
    await loadSyncTasks();
    await loadSyncStatus();
    startSyncPoll();
  };

  const onRouteLeave = async () => {
    stopSyncPoll();
  };

  window.HippoSync = {
    init,
    refresh,
    onRouteEnter,
    onRouteLeave,
    loadSyncStatus,
    loadSyncTasks,
    loadSyncSettings,
    loadLoginStatus,
  };
})();
