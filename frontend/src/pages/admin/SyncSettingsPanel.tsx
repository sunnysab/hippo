import type { Dispatch, SetStateAction } from 'react';
import { useSettingsState, type SyncSettings } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiGet, apiSend, isAuthError } from '../../api';
import { emitRefresh } from '../../utils/events';
import {
  buildSyncSettingsPayload,
  type SyncSettingsFormState,
} from '../settings/form';

interface SyncSettingsPanelProps {
  formState: SyncSettingsFormState;
  setFormState: Dispatch<SetStateAction<SyncSettingsFormState>>;
}

const isActiveTaskStatus = (status: string | undefined) => {
  return status === 'queued' || status === 'pending' || status === 'running';
};

export function SyncSettingsPanel({
  formState,
  setFormState,
}: SyncSettingsPanelProps) {
  const { dispatch } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();

  const watchTaskResult = async (taskId: string) => {
    const maxAttempts = 20;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      const payload = await apiGet(`/api/settings/tasks/${taskId}`);
      const status = String(payload.status || '');
      if (isActiveTaskStatus(status)) {
        continue;
      }
      emitRefresh();
      if (status === 'success') {
        showToast(t('sync.status.success', 'Success'));
        return;
      }
      showToast((payload.error as string) || t('sync.failed', 'Sync failed. Please check login.'));
      return;
    }
    emitRefresh();
  };

  const handleSave = async () => {
    try {
      const payload = await apiSend('/api/settings', 'PATCH', buildSyncSettingsPayload(formState));
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
      showToast(t('sync.saved', 'Sync settings saved.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('sync.saveFailed', 'Failed to save sync settings.'));
    }
  };


  return (
    <div className="panel sync-settings">
      <div className="panel-header">
        <div>
          <h2>{t('sync.title', 'Sync Settings')}</h2>
          <p className="muted">{t('sync.subtitle', 'Configure how account and article sync runs.')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-sync-save" type="button" onClick={handleSave}>
            {t('sync.save', 'Save')}
          </button>
          <button className="btn ghost" id="btn-sync-run" type="button" onClick={async () => {
            try {
              const payload = await apiSend('/api/settings/run', 'POST', {});
              const taskId = String(payload.task_id || '');
              const status = String(payload.status || '');
              showToast(
                taskId && isActiveTaskStatus(status)
                  ? t('sync.runQueued', 'Sync task queued')
                  : t('sync.runningProgress', 'Processing sync task'),
              );
              if (taskId) {
                void watchTaskResult(taskId);
              } else {
                emitRefresh();
              }
            } catch (err) {
              if (isAuthError(err)) return;
              showToast((err as Error)?.message || t('sync.runFailed', 'Failed to start sync task.'));
            }
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
                min="15"
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
