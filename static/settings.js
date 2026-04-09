(() => {
  const { state, $, apiGet, apiSend, t, activateTab, showToast } = window.Hippo;

  const HISTORY_PAGE_SIZE = 5;
  const SYNC_POLL_FAST_MS = 1000;
  const SYNC_POLL_IDLE_MS = 10000;
  const TEST_EMAIL_TIMEOUT_MS = 10000;
  const isNarrowViewport = () => window.matchMedia('(max-width: 760px)').matches;
  let lastSyncStatusFingerprint = '';
  let lastSyncTasksFingerprint = '';
  let syncSettingsCollapsed = true;
  let syncEmailCollapsed = true;
  let syncViewportWasNarrow = false;

  const buildSyncStatusFingerprint = (payload) => {
    if (!payload || typeof payload !== 'object') return '';
    const history = Array.isArray(payload.history) ? payload.history : [];
    const compactHistory = history.map((item) => ({
      started_at: item?.started_at || '',
      finished_at: item?.finished_at || '',
      status: item?.status || '',
      saved: Number(item?.saved || 0),
      downloaded: Number(item?.downloaded || 0),
      skipped_accounts: Number(item?.skipped_accounts || 0),
      error: item?.error || '',
    }));
    return JSON.stringify({
      status: payload.status || '',
      last_started_at: payload.last_started_at || '',
      last_finished_at: payload.last_finished_at || '',
      last_error: payload.last_error || '',
      history: compactHistory,
    });
  };

  const buildSyncTasksFingerprint = (tasks) => {
    if (!Array.isArray(tasks)) return '';
    const compactTasks = tasks.map((task) => ({
      task_id: task?.task_id || '',
      status: task?.status || '',
      created_at: task?.created_at || '',
      started_at: task?.started_at || '',
      finished_at: task?.finished_at || '',
      error: task?.error || '',
      accounts_total: Number(task?.accounts_total || 0),
      accounts_done: Number(task?.accounts_done || 0),
      last_log: task?.last_log || '',
      current_account_biz: task?.current_account?.biz || '',
      current_account_nickname: task?.current_account?.nickname || '',
      current_article_id: task?.current_article?.article_id || '',
      current_article_title: task?.current_article?.title || '',
      accounts: Array.isArray(task?.accounts)
        ? task.accounts.map((account) => ({
            biz: account?.biz || '',
            status: account?.status || '',
            saved: Number(account?.saved || 0),
            page_count: Number(account?.page_count || 0),
            article_current:
              account?.article_current === null || account?.article_current === undefined
                ? null
                : Number(account.article_current),
            article_total:
              account?.article_total === null || account?.article_total === undefined
                ? null
                : Number(account.article_total),
            last_article_id: account?.last_article?.article_id || '',
            updated_at: account?.updated_at || '',
            skip_reason: account?.skip_reason || '',
          }))
        : [],
    }));
    return JSON.stringify(compactTasks);
  };

  const escapeHtml = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (char) => {
      if (char === '&') return '&amp;';
      if (char === '<') return '&lt;';
      if (char === '>') return '&gt;';
      if (char === '"') return '&quot;';
      return '&#39;';
    });

  const normalizeWindowStartHour = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 6;
    return Math.min(Math.max(Math.trunc(numeric), 0), 23);
  };

  const normalizeWindowEndHour = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 24;
    return Math.min(Math.max(Math.trunc(numeric), 0), 24);
  };

  const formatHour = (value) => `${String(value).padStart(2, '0')}:00`;

  const renderWindowHint = (startHour, endHour) => {
    const note = $('#sync-window-note');
    if (!note) return;
    if (startHour === endHour) {
      note.textContent = t('sync.windowHintAllDay', 'Current window: all day');
      return;
    }
    note.textContent = t('sync.windowHintRange', 'Current window: {start} - {end}')
      .replace('{start}', formatHour(startHour))
      .replace('{end}', formatHour(endHour));
  };

  const syncWindowInputs = (startValue, endValue) => {
    const startHour = normalizeWindowStartHour(startValue);
    const endHour = normalizeWindowEndHour(endValue);
    const startInput = $('#sync-window-start');
    const endInput = $('#sync-window-end');
    if (startInput) startInput.value = startHour;
    if (endInput) endInput.value = endHour;
    renderWindowHint(startHour, endHour);
    return { startHour, endHour };
  };

  const getActiveTask = () => {
    const tasks = state.syncTasks || [];
    return tasks.find((task) => task.status === 'running') || tasks.find((task) => task.status === 'pending') || null;
  };

  const getPhaseLabel = (phase) => {
    if (!phase) return '';
    return t(`sync.phase.${phase}`, phase);
  };

  const getActiveTaskStage = (task) => {
    if (!task) return null;
    if (task.status === 'pending') {
      return t('sync.pendingHint', 'Waiting for running sync slot');
    }
    const phase = task.phase || (task.last_log === 'downloading_images' ? 'images' : null);
    if (!phase) return null;
    return t('sync.phaseLine', 'Stage: {stage}').replace('{stage}', getPhaseLabel(phase));
  };

  const getActiveTaskDetail = (task) => {
    if (!task) return null;
    if (task.status === 'pending') return null;
    if (task.phase === 'images') return null;
    if (task.current_article?.title) {
      return t('sync.runningArticle', 'Processing {title}').replace('{title}', task.current_article.title);
    }
    return null;
  };

  const getActiveTaskAccountName = (task) => {
    if (!task) return t('sync.runningProgress', 'Processing sync task');
    const isPending = task.status === 'pending';
    const currentAccount = task.current_account || {};
    if (currentAccount.nickname || currentAccount.biz) {
      return currentAccount.nickname || currentAccount.biz;
    }
    if (isPending) {
      return t('sync.pendingUnknown', 'Waiting for current sync to finish');
    }
    if ((task.accounts_total || 0) === 0 && !task.phase && !task.last_log) {
      return t('sync.runningPreparing', 'Preparing sync task');
    }
    return t('sync.runningProgress', 'Processing sync task');
  };

  const getAccountStatusWeight = (status) => {
    if (status === 'running') return 0;
    if (status === 'pending') return 1;
    if (status === 'completed') return 2;
    if (status === 'failed') return 3;
    if (status === 'skipped') return 4;
    if (status === 'stopped') return 5;
    return 5;
  };

  const getSkipReasonLabel = (reason) => {
    if (!reason) return '';
    const key = `sync.skip.${reason}`;
    const label = t(key, '');
    return label === key ? reason : label;
  };

  const getSyncTone = (status) => {
    if (status === 'success' || status === 'completed') return 'success';
    if (status === 'failed' || status === 'login_required' || status === 'error' || status === 'stopped') return 'danger';
    if (status === 'running') return 'info';
    if (status === 'pending') return 'warning';
    if (status === 'skipped') return 'muted';
    return 'neutral';
  };

  const getLoginToneStatus = (status) => {
    if (status === 'success') return 'completed';
    if (status === 'error') return 'failed';
    if (loginActionStatuses.includes(status)) return 'running';
    return 'idle';
  };

  const renderActiveTask = () => {
    const container = $('#sync-active-task');
    if (!container) return;
    const activeTask = getActiveTask();
    if (!activeTask) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(t('sync.activeEmpty', 'No active sync task.'))}</div>`;
      return;
    }

    const isPending = activeTask.status === 'pending';
    const accountName = getActiveTaskAccountName(activeTask);
    const startedAt = activeTask.started_at || activeTask.created_at;
    const minutes = startedAt
      ? Math.max(1, Math.floor((Date.now() - new Date(startedAt).getTime()) / 60000))
      : 0;
    const progressBadge = activeTask.accounts_total > 0 ? `${activeTask.accounts_done || 0}/${activeTask.accounts_total}` : '';
    const progressPercent = activeTask.accounts_total > 0
      ? Math.max(4, Math.round(((activeTask.accounts_done || 0) / activeTask.accounts_total) * 100))
      : 0;
    const stage = getActiveTaskStage(activeTask);
    const detail = getActiveTaskDetail(activeTask);
    const statusLabel = t(`sync.status.${activeTask.status || 'running'}`, activeTask.status || 'running');
    const toneClass = `sync-tone-${getSyncTone(activeTask.status)}`;
    const accounts = Array.isArray(activeTask.accounts) ? [...activeTask.accounts] : [];
    accounts.sort((left, right) => {
      const currentBiz = activeTask.current_account?.biz || '';
      if (left.biz === currentBiz && right.biz !== currentBiz) return -1;
      if (right.biz === currentBiz && left.biz !== currentBiz) return 1;
      const statusDiff = getAccountStatusWeight(left.status) - getAccountStatusWeight(right.status);
      if (statusDiff !== 0) return statusDiff;
      return (right.updated_at || '').localeCompare(left.updated_at || '');
    });
    const visibleAccounts = accounts.slice(0, 8);
    const hiddenCount = Math.max(accounts.length - visibleAccounts.length, 0);

    const accountRows = visibleAccounts
      .map((account) => {
        const statusLabel = t(`sync.status.${account.status || 'pending'}`, account.status || 'pending');
        const accountToneClass = `sync-tone-${getSyncTone(account.status)}`;
        const articleBadge =
          account.article_total !== null && account.article_total !== undefined
            ? `${account.article_current || 0}/${account.article_total}`
            : account.article_current
              ? `${account.article_current}`
              : '';
        const meta = [];
        if (account.status === 'running' && account.phase) {
          meta.push(t('sync.phaseLine', 'Stage: {stage}').replace('{stage}', getPhaseLabel(account.phase)));
        }
        if (account.saved) {
          meta.push(t('sync.accountSaved', 'Saved {n} articles').replace('{n}', account.saved));
        }
        if (account.page_count) {
          meta.push(t('sync.accountPages', 'Fetched {n} pages').replace('{n}', account.page_count));
        }
        if (account.updated_at) {
          meta.push(formatRelativeTime(account.updated_at));
        }
        if (account.skip_reason) {
          meta.push(getSkipReasonLabel(account.skip_reason));
        }
        if (account.error) {
          meta.push(t('sync.accountError', 'Error: {message}').replace('{message}', account.error));
        }
        const lastArticleTitle = account.last_article?.title
          ? t('sync.accountLastArticle', 'Latest article: {title}').replace('{title}', account.last_article.title)
          : '';
        return `
          <div class="sync-progress-item ${account.status === 'running' ? 'is-running' : ''} ${accountToneClass}">
            <div class="sync-progress-copy">
              <div class="sync-progress-title-row">
                <div class="account-name">${escapeHtml(account.nickname || account.biz || '-')}</div>
                <span class="sync-progress-status sync-status-badge ${accountToneClass}">${escapeHtml(statusLabel)}</span>
              </div>
              ${meta.length ? `<div class="account-sub">${escapeHtml(meta.join(' · '))}</div>` : ''}
              ${lastArticleTitle ? `<div class="account-sub">${escapeHtml(lastArticleTitle)}</div>` : ''}
            </div>
            ${articleBadge ? `<span class="badge">${escapeHtml(articleBadge)}</span>` : ''}
          </div>
        `;
      })
      .join('');

    const summaryPills = [stage, detail].filter(Boolean);

    container.innerHTML = `
      <div class="sync-active-summary">
        <div class="sync-summary-copy">
          <div class="sync-summary-top">
            <div class="account-name">${escapeHtml(
              t(isPending ? 'sync.pendingTitle' : 'sync.runningTitle', isPending ? 'Queued {name}' : 'Syncing {name}')
                .replace('{name}', accountName)
            )}</div>
            <span class="sync-status-badge ${toneClass}">${escapeHtml(statusLabel)}</span>
          </div>
          <div class="account-sub">${escapeHtml(
            t(
              isPending ? 'sync.pendingSince' : 'sync.runningSince',
              isPending ? 'Queued for {n} minutes' : 'Started {n} minutes ago'
            ).replace('{n}', minutes)
          )}</div>
          ${
            summaryPills.length
              ? `<div class="sync-summary-pills">${summaryPills.map((item) => `<span class="sync-inline-pill">${escapeHtml(item)}</span>`).join('')}</div>`
              : ''
          }
        </div>
        <div class="sync-summary-side">
          ${progressBadge ? `<span class="badge">${escapeHtml(progressBadge)}</span>` : ''}
        </div>
      </div>
      ${progressBadge ? `<div class="sync-progress-track" aria-hidden="true"><span style="width: ${progressPercent}%"></span></div>` : ''}
      ${accountRows ? `<div class="sync-progress-list">${accountRows}</div>` : ''}
      ${
        hiddenCount
          ? `<div class="muted sync-progress-more">${escapeHtml(
              t('sync.accountMore', '{n} more accounts').replace('{n}', hiddenCount)
            )}</div>`
          : ''
      }
    `;
  };

  const renderSyncHistory = () => {
    const list = $('#sync-history');
    if (!list) return;
    renderActiveTask();
    list.innerHTML = '';
    const history = state.syncStatus?.history || [];
    const activeTask = getActiveTask();
    if (!history.length && !activeTask) {
      list.innerHTML = `<div class="empty-state">${t('sync.historyEmpty', 'No sync history.')}</div>`;
      return;
    }
    if (activeTask) {
      const item = document.createElement('div');
      const activeToneClass = `sync-tone-${getSyncTone(activeTask.status)}`;
      item.className = `list-item sync-history-item is-running ${activeToneClass}`;
      const isPending = activeTask.status === 'pending';
      const accountName = getActiveTaskAccountName(activeTask);
      const startedAt = activeTask.started_at || activeTask.created_at;
      const minutes = startedAt
        ? Math.max(1, Math.floor((Date.now() - new Date(startedAt).getTime()) / 60000))
        : 0;
      const progressBadge =
        activeTask.accounts_total > 0
          ? `${activeTask.accounts_done || 0}/${activeTask.accounts_total}`
          : '';
      const stage = getActiveTaskStage(activeTask);
      const detail = getActiveTaskDetail(activeTask);
      const statusLabel = t(`sync.status.${activeTask.status || 'running'}`, activeTask.status || 'running');
      item.innerHTML = `
        <div class="sync-history-copy">
          <div class="sync-history-top">
            <div class="account-name">${escapeHtml(t(isPending ? 'sync.pendingTitle' : 'sync.runningTitle', isPending ? 'Queued {name}' : 'Syncing {name}').replace('{name}', accountName))}</div>
            <span class="sync-status-badge ${activeToneClass}">${escapeHtml(statusLabel)}</span>
          </div>
          <div class="account-sub">${escapeHtml(t(isPending ? 'sync.pendingSince' : 'sync.runningSince', isPending ? 'Queued for {n} minutes' : 'Started {n} minutes ago').replace('{n}', minutes))}</div>
          ${stage ? `<div class="account-sub">${escapeHtml(stage)}</div>` : ''}
          ${detail ? `<div class="account-sub">${escapeHtml(detail)}</div>` : ''}
        </div>
        ${progressBadge ? `<span class="badge">${escapeHtml(progressBadge)}</span>` : ''}
      `;
      list.appendChild(item);
    }
    const paged = history.slice(0, HISTORY_PAGE_SIZE);
    paged.forEach((entry) => {
      const item = document.createElement('div');
      const toneClass = `sync-tone-${getSyncTone(entry.status || 'unknown')}`;
      item.className = `list-item sync-history-item ${toneClass}`;
      const status = entry.status || 'unknown';
      const saved = entry.saved || 0;
      const started = entry.started_at ? new Date(entry.started_at).toLocaleString('zh-CN') : '-';
      item.innerHTML = `
        <div class="sync-history-copy">
          <div class="sync-history-top">
            <div class="account-name">${escapeHtml(t(`sync.status.${status}`, status))}</div>
            <span class="sync-status-badge ${toneClass}">${escapeHtml(t(`sync.status.${status}`, status))}</span>
          </div>
          <div class="account-sub">${escapeHtml(started)}</div>
          ${entry.error ? `<div class="account-sub">${escapeHtml(entry.error)}</div>` : ''}
        </div>
        <span class="badge">+${escapeHtml(saved)}</span>
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
    const payload = await apiGet('/api/settings/status');
    const fingerprint = buildSyncStatusFingerprint(payload);
    if (fingerprint === lastSyncStatusFingerprint) {
      return;
    }
    lastSyncStatusFingerprint = fingerprint;
    state.syncStatus = payload;
    renderSyncStatus();
  };

  const loadSyncTasks = async () => {
    const payload = await apiGet('/api/settings/tasks?limit=5&detail=true');
    const tasks = payload.tasks || [];
    const fingerprint = buildSyncTasksFingerprint(tasks);
    if (fingerprint === lastSyncTasksFingerprint) {
      return;
    }
    lastSyncTasksFingerprint = fingerprint;
    state.syncTasks = tasks;
    renderSyncHistory();
  };

  const renderSyncSettings = (settings) => {
    if (!settings) return;
    $('#sync-enabled').checked = Boolean(settings.enabled);
    $('#sync-interval').value = settings.interval_minutes ?? 60;
    syncWindowInputs(settings.window_start_hour ?? 6, settings.window_end_hour ?? 24);
    $('#sync-sleep').value = settings.sleep_seconds ?? 0.05;
    $('#sync-skip-minutes').value = settings.skip_minutes ?? 30;
    $('#sync-download-content').checked = Boolean(settings.download_content);
    $('#sync-download-images').checked = Boolean(settings.download_images);
    $('#sync-article-exclude-keywords').value = settings.article_exclude_keywords || '';
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

  const renderSyncMobilePanelState = (
    panelSelector,
    buttonSelector,
    collapsed,
    showKey,
    hideKey,
    showFallback,
    hideFallback,
  ) => {
    const narrow = isNarrowViewport();
    const panel = $(panelSelector);
    const button = $(buttonSelector);
    if (panel) {
      panel.classList.toggle('is-mobile-collapsed', narrow && collapsed);
    }
    if (!button) return;
    button.classList.toggle('is-hidden', !narrow);
    button.textContent = t(
      collapsed ? showKey : hideKey,
      collapsed ? showFallback : hideFallback,
    );
    button.setAttribute('aria-expanded', String(!(narrow && collapsed)));
  };

  const renderSyncMobileLayout = () => {
    renderSyncMobilePanelState(
      '.sync-settings',
      '#btn-sync-settings-toggle',
      syncSettingsCollapsed,
      'sync.showSettings',
      'sync.hideSettings',
      'Show settings',
      'Hide settings',
    );
    renderSyncMobilePanelState(
      '.sync-email',
      '#btn-sync-email-toggle',
      syncEmailCollapsed,
      'email.showSettings',
      'email.hideSettings',
      'Show settings',
      'Hide settings',
    );
  };

  const syncMobileLayout = (options = {}) => {
    const forceCollapse = Boolean(options.forceCollapse);
    const narrow = isNarrowViewport();
    if (!narrow) {
      syncSettingsCollapsed = false;
      syncEmailCollapsed = false;
    } else if (!syncViewportWasNarrow || forceCollapse) {
      syncSettingsCollapsed = true;
      syncEmailCollapsed = true;
    }
    syncViewportWasNarrow = narrow;
    renderSyncMobileLayout();
  };

  const loadSyncSettings = async () => {
    const payload = await apiGet('/api/settings');
    state.syncSettings = payload;
    renderSyncSettings(payload);
  };

  const getEmailFormData = () => ({
    smtp_host: $('#email-smtp-host').value.trim(),
    smtp_port: Number($('#email-smtp-port').value),
    smtp_user: $('#email-smtp-user').value.trim(),
    smtp_password: $('#email-smtp-password').value,
    smtp_tls: $('#email-tls').checked,
    from_email: $('#email-from').value.trim(),
  });

  const getEmailSettingsBody = () => ({
    alert_enabled: $('#sync-alert-enabled').checked,
    alert_email: $('#sync-alert-email').value.trim(),
    email: getEmailFormData(),
  });

  const saveSyncSettings = async () => {
    const { startHour, endHour } = syncWindowInputs(
      $('#sync-window-start').value,
      $('#sync-window-end').value,
    );
    const body = {
      enabled: $('#sync-enabled').checked,
      interval_minutes: Number($('#sync-interval').value),
      window_start_hour: startHour,
      window_end_hour: endHour,
      sleep_seconds: Number($('#sync-sleep').value),
      skip_minutes: Number($('#sync-skip-minutes').value),
      download_content: $('#sync-download-content').checked,
      download_images: $('#sync-download-images').checked,
      article_exclude_keywords: $('#sync-article-exclude-keywords').value.trim(),
      alert_enabled: $('#sync-alert-enabled').checked,
      alert_email: $('#sync-alert-email').value.trim(),
      email: getEmailFormData(),
    };
    const payload = await apiSend('/api/settings', 'PATCH', body);
    state.syncSettings = payload;
    renderSyncSettings(payload);
  };

  const saveEmailSettings = async () => {
    const btn = $('#btn-email-save');
    if (btn) btn.disabled = true;
    try {
      const payload = await apiSend('/api/settings', 'PATCH', getEmailSettingsBody());
      state.syncSettings = payload;
      renderSyncSettings(payload);
      showToast(t('email.saveSuccess', 'Email settings saved.'));
    } catch (err) {
      console.warn('Failed to save email settings', err);
      showToast(err?.message || t('email.saveFailed', 'Failed to save email settings.'));
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  const setEmailTestButtonLoading = (isLoading) => {
    const btn = $('#btn-email-test');
    const spinner = $('#email-test-spinner');
    const label = $('#email-test-label');
    if (btn) btn.disabled = isLoading;
    if (spinner) spinner.classList.toggle('is-hidden', !isLoading);
    if (label) {
      label.textContent = isLoading
        ? t('email.testing', 'Sending...')
        : t('email.test', 'Test');
    }
  };

  const triggerTestEmail = async () => {
    setEmailTestButtonLoading(true);
    try {
      const payload = await apiSend('/api/settings/test-email', 'POST', {
        to_email: $('#sync-alert-email').value.trim(),
        email: getEmailFormData(),
      }, { timeoutMs: TEST_EMAIL_TIMEOUT_MS });
      const toEmail = payload?.to_email || $('#sync-alert-email').value.trim();
      showToast(
        t('email.testSuccess', 'Test email sent to {email}.').replace('{email}', toEmail || '-'),
      );
    } catch (err) {
      console.warn('Failed to send test email', err);
      if (err?.code === 'TIMEOUT') {
        showToast(t('email.testTimeout', 'Timed out while sending test email.'));
      } else {
        showToast(err?.message || t('email.testFailed', 'Failed to send test email.'));
      }
    } finally {
      setEmailTestButtonLoading(false);
    }
  };

  const triggerSyncRun = async () => {
    await apiSend('/api/settings/run', 'POST', {});
    setTimeout(async () => {
      await loadSyncTasks();
      await loadSyncStatus();
    }, 1000);
  };

  const renderLoginStatus = (payload) => {
    state.loginStatus = payload;
    const status = payload.status || 'idle';
    const statusText = t(`login.status.${status}`, payload.message || status);
    const loginStatus = $('#login-status');
    if (loginStatus) {
      loginStatus.textContent = statusText;
      loginStatus.className = `login-status sync-status-badge sync-tone-${getSyncTone(getLoginToneStatus(status))}`;
    }
    const qr = $('#login-qr');
    const loginCard = $('#view-settings .login-card');
    if (loginCard) {
      loginCard.dataset.status = status;
    }
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
    const currentStatus = state.loginStatus?.status || 'idle';
    const force = loginActionStatuses.includes(currentStatus);
    const payload = await apiSend('/api/login/start', 'POST', { force });
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
    const syncSettingsToggle = $('#btn-sync-settings-toggle');
    if (syncSettingsToggle) {
      syncSettingsToggle.addEventListener('click', () => {
        syncSettingsCollapsed = !syncSettingsCollapsed;
        renderSyncMobileLayout();
      });
    }
    const syncEmailToggle = $('#btn-sync-email-toggle');
    if (syncEmailToggle) {
      syncEmailToggle.addEventListener('click', () => {
        syncEmailCollapsed = !syncEmailCollapsed;
        renderSyncMobileLayout();
      });
    }
    const emailSaveBtn = $('#btn-email-save');
    if (emailSaveBtn) {
      emailSaveBtn.addEventListener('click', saveEmailSettings);
    }
    const emailTestBtn = $('#btn-email-test');
    if (emailTestBtn) {
      emailTestBtn.addEventListener('click', triggerTestEmail);
    }
    const emailTls = $('#email-tls');
    if (emailTls) {
      emailTls.addEventListener('change', () => {
        const port = $('#email-smtp-port');
        if (!port) return;
        port.value = emailTls.checked ? '587' : '25';
      });
    }
    const startInput = $('#sync-window-start');
    const endInput = $('#sync-window-end');
    if (startInput && endInput) {
      startInput.addEventListener('change', () => {
        syncWindowInputs(startInput.value, endInput.value);
      });
      endInput.addEventListener('change', () => {
        syncWindowInputs(startInput.value, endInput.value);
      });
    }
    window.addEventListener('resize', () => {
      syncMobileLayout();
    });
  };

  const isSyncActive = () => $('#view-settings')?.classList.contains('is-active');

  const hasActiveSyncTask = () => {
    const tasks = state.syncTasks || [];
    if (tasks.some((task) => task?.status === 'running' || task?.status === 'pending')) {
      return true;
    }
    return state.syncStatus?.status === 'running' || state.syncStatus?.status === 'pending';
  };

  const getSyncPollDelay = () => (hasActiveSyncTask() ? SYNC_POLL_FAST_MS : SYNC_POLL_IDLE_MS);

  const scheduleNextSyncPoll = (delay) => {
    if (state.syncPollTimer) {
      clearTimeout(state.syncPollTimer);
    }
    state.syncPollTimer = setTimeout(async () => {
      if (!isSyncActive()) {
        scheduleNextSyncPoll(SYNC_POLL_IDLE_MS);
        return;
      }
      try {
        await loadSyncTasks();
        await loadSyncStatus();
      } catch (err) {
        console.warn('Sync poll failed', err);
      }
      scheduleNextSyncPoll(getSyncPollDelay());
    }, Math.max(delay, 500));
  };

  const startSyncPoll = () => {
    scheduleNextSyncPoll(getSyncPollDelay());
  };

  const stopSyncPoll = () => {
    if (!state.syncPollTimer) return;
    clearTimeout(state.syncPollTimer);
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
    syncMobileLayout({ forceCollapse: true });
    await refresh();
  };

  const onRouteEnter = async () => {
    syncMobileLayout();
    await loadSyncTasks();
    await loadSyncStatus();
    startSyncPoll();
  };

  const onRouteLeave = async () => {
    stopSyncPoll();
  };

  window.HippoSettings = {
    init,
    refresh,
    onRouteEnter,
    onRouteLeave,
    startLogin,
    loadSyncStatus,
    loadSyncTasks,
    loadSyncSettings,
    loadLoginStatus,
    getActiveTaskAccountName,
  };
})();
