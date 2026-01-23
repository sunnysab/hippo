(() => {
  const { state, $, apiGet, apiSend, t, activateTab } = window.Hippo;

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
    $('#email-smtp-host').value = email.smtp_host || '';
    $('#email-smtp-port').value = email.smtp_port ?? 587;
    $('#email-smtp-user').value = email.smtp_user || '';
    $('#email-smtp-password').value = email.smtp_password || '';
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
    $('#btn-login-refresh').addEventListener('click', startLogin);
    $('#btn-sync-save').addEventListener('click', saveSyncSettings);
    $('#btn-sync-run').addEventListener('click', triggerSyncRun);
    $('#btn-banner-login').addEventListener('click', async () => {
      activateTab('sync');
      await startLogin();
    });
  };

  const refresh = async () => {
    await loadSyncStatus();
    await loadSyncSettings();
    await loadLoginStatus();
  };

  const init = async () => {
    bindEvents();
    await refresh();
  };

  window.HippoSync = {
    init,
    refresh,
    loadSyncStatus,
    loadSyncSettings,
    loadLoginStatus,
  };
})();
