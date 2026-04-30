import type { Dispatch, SetStateAction } from 'react';
import { useSettingsState, type SyncSettings } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend } from '../../api';
import {
  buildSyncSettingsPayload,
  type SyncSettingsFormState,
} from './form';

interface SyncSettingsPanelProps {
  formState: SyncSettingsFormState;
  setFormState: Dispatch<SetStateAction<SyncSettingsFormState>>;
}

export function SyncSettingsPanel({
  formState,
  setFormState,
}: SyncSettingsPanelProps) {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const settings = state.syncSettings;

  const handleSave = async () => {
    const payload = await apiSend('/api/settings', 'PATCH', buildSyncSettingsPayload(formState));
    dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
    showToast(t('sync.saved', 'Sync settings saved.'));
  };

  const formatHour = (h: number) => `${String(h).padStart(2, '0')}:00`;

  return (
    <div className="panel sync-settings">
      <div className="panel-header">
        <div>
          <h2>{t('sync.title', 'Sync Settings')}</h2>
          <p className="muted">{t('sync.subtitle', 'Schedule periodic sync for accounts and articles.')}</p>
        </div>
        <div className="toolbar">
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
      <div className="sync-form-sections">
        <section className="sync-form-section">
          <div className="sync-section-title">{t('sync.sectionSchedule', 'Schedule')}</div>
          <div className="form-grid">
            <label className="switch">
              <span>{t('sync.enabled', 'Enable automatic sync')}</span>
              <input
                type="checkbox"
                id="sync-enabled"
                checked={formState.enabled}
                onChange={(event) => setFormState((prev) => ({ ...prev, enabled: event.target.checked }))}
              />
            </label>
            <label>
              <span>{t('sync.interval', 'Interval (minutes)')}</span>
              <input
                type="number"
                id="sync-interval"
                min="1"
                value={formState.intervalMinutes}
                onChange={(event) => setFormState((prev) => ({ ...prev, intervalMinutes: event.target.value }))}
              />
            </label>
            <label className="sync-window-field">
              <span>{t('sync.windowRange', 'Sync window')}</span>
              <div className="sync-window-inputs">
                <input
                  type="number"
                  id="sync-window-start"
                  min="0"
                  max="23"
                  value={formState.windowStartHour}
                  onChange={(event) => setFormState((prev) => ({ ...prev, windowStartHour: event.target.value }))}
                />
                <span className="sync-window-unit">{t('sync.windowHour', 'h')}</span>
                <span className="sync-window-separator">{t('sync.windowTo', 'to')}</span>
                <input
                  type="number"
                  id="sync-window-end"
                  min="0"
                  max="24"
                  value={formState.windowEndHour}
                  onChange={(event) => setFormState((prev) => ({ ...prev, windowEndHour: event.target.value }))}
                />
                <span className="sync-window-unit">{t('sync.windowHour', 'h')}</span>
              </div>
            </label>
            <p className="muted sync-window-note" id="sync-window-note">
              {t('sync.windowHintRange', 'Current window: {start} - {end}')
                .replace('{start}', formatHour(Number(formState.windowStartHour || settings?.window_start_hour || 6)))
                .replace('{end}', formatHour(Number(formState.windowEndHour || settings?.window_end_hour || 24)))}
            </p>
          </div>
        </section>
        <section className="sync-form-section">
          <div className="sync-section-title">{t('sync.sectionFetch', 'Fetch')}</div>
          <div className="form-grid">
            <label>
              <span>{t('sync.sleepSeconds', 'Sleep (seconds)')}</span>
              <input
                type="number"
                id="sync-sleep"
                min="0"
                step="0.01"
                value={formState.sleepSeconds}
                onChange={(event) => setFormState((prev) => ({ ...prev, sleepSeconds: event.target.value }))}
              />
            </label>
            <label>
              <span>{t('sync.skipMinutes', 'Skip interval (minutes)')}</span>
              <input
                type="number"
                id="sync-skip-minutes"
                min="0"
                value={formState.skipMinutes}
                onChange={(event) => setFormState((prev) => ({ ...prev, skipMinutes: event.target.value }))}
              />
            </label>
            <label className="switch">
              <span>{t('sync.downloadContent', 'Download content')}</span>
              <input
                type="checkbox"
                id="sync-download-content"
                checked={formState.downloadContent}
                onChange={(event) => setFormState((prev) => ({ ...prev, downloadContent: event.target.checked }))}
              />
            </label>
            <label className="switch">
              <span>{t('sync.downloadImages', 'Download images')}</span>
              <input
                type="checkbox"
                id="sync-download-images"
                checked={formState.downloadImages}
                onChange={(event) => setFormState((prev) => ({ ...prev, downloadImages: event.target.checked }))}
              />
            </label>
          </div>
        </section>
      </div>
    </div>
  );
}
