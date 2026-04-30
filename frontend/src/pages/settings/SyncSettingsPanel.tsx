import { useState, useEffect } from 'react';
import { useSettingsState, type SyncSettings } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend } from '../../api';

export function SyncSettingsPanel() {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [collapsed, setCollapsed] = useState(true);
  const isNarrow = window.matchMedia('(max-width: 760px)').matches;

  const settings = state.syncSettings;

  useEffect(() => {
    if (!settings) return;
    const enabled = document.getElementById('sync-enabled') as HTMLInputElement | null;
    const interval = document.getElementById('sync-interval') as HTMLInputElement | null;
    const windowStart = document.getElementById('sync-window-start') as HTMLInputElement | null;
    const windowEnd = document.getElementById('sync-window-end') as HTMLInputElement | null;
    const sleep = document.getElementById('sync-sleep') as HTMLInputElement | null;
    const skip = document.getElementById('sync-skip-minutes') as HTMLInputElement | null;
    const downloadContent = document.getElementById('sync-download-content') as HTMLInputElement | null;
    const downloadImages = document.getElementById('sync-download-images') as HTMLInputElement | null;
    const articleKeywords = document.getElementById('sync-article-exclude-keywords') as HTMLTextAreaElement | null;

    if (enabled) enabled.checked = Boolean(settings.enabled);
    if (interval) interval.value = String(settings.interval_minutes ?? 60);
    if (windowStart) windowStart.value = String(settings.window_start_hour ?? 6);
    if (windowEnd) windowEnd.value = String(settings.window_end_hour ?? 24);
    if (sleep) sleep.value = String(settings.sleep_seconds ?? 0.05);
    if (skip) skip.value = String(settings.skip_minutes ?? 30);
    if (downloadContent) downloadContent.checked = Boolean(settings.download_content);
    if (downloadImages) downloadImages.checked = Boolean(settings.download_images);
    if (articleKeywords) articleKeywords.value = settings.article_exclude_keywords || '';
  }, [settings]);

  useEffect(() => {
    const emailToggle = document.getElementById('sync-alert-enabled') as HTMLInputElement | null;
    const alertEmail = document.getElementById('sync-alert-email') as HTMLInputElement | null;
    if (!settings) return;
    if (emailToggle) emailToggle.checked = Boolean(settings.alert_enabled);
    if (alertEmail) alertEmail.value = settings.alert_email || '';
  }, [settings]);

  const handleSave = async () => {
    const enabled = (document.getElementById('sync-enabled') as HTMLInputElement)?.checked;
    const interval = Number((document.getElementById('sync-interval') as HTMLInputElement)?.value);
    const windowStartVal = Number((document.getElementById('sync-window-start') as HTMLInputElement)?.value);
    const windowEndVal = Number((document.getElementById('sync-window-end') as HTMLInputElement)?.value);
    const sleep = Number((document.getElementById('sync-sleep') as HTMLInputElement)?.value);
    const skip = Number((document.getElementById('sync-skip-minutes') as HTMLInputElement)?.value);
    const downloadContent = (document.getElementById('sync-download-content') as HTMLInputElement)?.checked;
    const downloadImages = (document.getElementById('sync-download-images') as HTMLInputElement)?.checked;
    const articleKeywords = (document.getElementById('sync-article-exclude-keywords') as HTMLTextAreaElement)?.value.trim();

    const emailEnabled = (document.getElementById('sync-alert-enabled') as HTMLInputElement)?.checked;
    const alertEmailVal = (document.getElementById('sync-alert-email') as HTMLInputElement)?.value.trim();

    const body: Record<string, unknown> = {
      enabled,
      interval_minutes: interval,
      window_start_hour: windowStartVal,
      window_end_hour: windowEndVal,
      sleep_seconds: sleep,
      skip_minutes: skip,
      download_content: downloadContent,
      download_images: downloadImages,
      article_exclude_keywords: articleKeywords || '',
      alert_enabled: emailEnabled,
      alert_email: alertEmailVal || '',
    };

    const payload = await apiSend('/api/settings', 'PATCH', body);
    dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
  };

  const formatHour = (h: number) => `${String(h).padStart(2, '0')}:00`;

  return (
    <div className="panel sync-settings">
      <div className="panel-header">
        <div>
          <div className="section-kicker">{t('sync.settingsKicker', 'Sync Policy')}</div>
          <h2>{t('sync.title', 'Sync Settings')}</h2>
          <p className="muted">{t('sync.subtitle', 'Schedule periodic sync for accounts and articles.')}</p>
        </div>
        <div className="toolbar">
          {isNarrow && (
            <button
              className="btn ghost sync-mobile-toggle"
              id="btn-sync-settings-toggle"
              type="button"
              onClick={() => setCollapsed(!collapsed)}
              aria-expanded={!collapsed}
            >
              {collapsed ? t('sync.showSettings', 'Show settings') : t('sync.hideSettings', 'Hide settings')}
            </button>
          )}
          <button className="btn" id="btn-sync-save" type="button" onClick={handleSave}>
            {t('sync.save', 'Save')}
          </button>
          <button className="btn ghost" id="btn-sync-run" type="button" onClick={async () => {
            await apiSend('/api/settings/run', 'POST', {});
            showToast(t('sync.runningProgress', 'Processing sync task'));
          }}>
            {t('sync.run', 'Run Now')}
          </button>
        </div>
      </div>
      <div className={`sync-form-sections${isNarrow && collapsed ? ' is-mobile-collapsed' : ''}`}>
        <section className="sync-form-section">
          <div className="sync-section-title">{t('sync.sectionSchedule', 'Schedule')}</div>
          <div className="form-grid">
            <label className="switch">
              <span>{t('sync.enabled', 'Enable automatic sync')}</span>
              <input type="checkbox" id="sync-enabled" />
            </label>
            <label>
              <span>{t('sync.interval', 'Interval (minutes)')}</span>
              <input type="number" id="sync-interval" min="1" />
            </label>
            <label className="sync-window-field">
              <span>{t('sync.windowRange', 'Sync window')}</span>
              <div className="sync-window-inputs">
                <input type="number" id="sync-window-start" min="0" max="23" />
                <span className="sync-window-unit">{t('sync.windowHour', 'h')}</span>
                <span className="sync-window-separator">{t('sync.windowTo', 'to')}</span>
                <input type="number" id="sync-window-end" min="0" max="24" />
                <span className="sync-window-unit">{t('sync.windowHour', 'h')}</span>
              </div>
            </label>
            <p className="muted sync-window-note" id="sync-window-note">
              {t('sync.windowHintRange', 'Current window: {start} - {end}')
                .replace('{start}', formatHour(settings?.window_start_hour ?? 6))
                .replace('{end}', formatHour(settings?.window_end_hour ?? 24))}
            </p>
          </div>
        </section>
        <section className="sync-form-section">
          <div className="sync-section-title">{t('sync.sectionFetch', 'Fetch')}</div>
          <div className="form-grid">
            <label>
              <span>{t('sync.sleepSeconds', 'Sleep (seconds)')}</span>
              <input type="number" id="sync-sleep" min="0" step="0.01" />
            </label>
            <label>
              <span>{t('sync.skipMinutes', 'Skip interval (minutes)')}</span>
              <input type="number" id="sync-skip-minutes" min="0" />
            </label>
            <label className="switch">
              <span>{t('sync.downloadContent', 'Download content')}</span>
              <input type="checkbox" id="sync-download-content" />
            </label>
            <label className="switch">
              <span>{t('sync.downloadImages', 'Download images')}</span>
              <input type="checkbox" id="sync-download-images" />
            </label>
          </div>
        </section>
        <section className="sync-form-section">
          <div className="sync-section-title">{t('sync.sectionFilter', 'Filter')}</div>
          <div className="form-grid">
            <label className="sync-textarea-field">
              <span>{t('sync.articleExcludeKeywords', 'Exclude article keywords')}</span>
              <textarea
                id="sync-article-exclude-keywords"
                rows={4}
                placeholder="promo&#10;ad"
              ></textarea>
              <small className="muted">{t('sync.articleExcludeKeywordsHint', 'One keyword per line, or separate by comma / semicolon.')}</small>
            </label>
          </div>
        </section>
      </div>
    </div>
  );
}
