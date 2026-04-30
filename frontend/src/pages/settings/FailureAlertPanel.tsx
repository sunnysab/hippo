import type { Dispatch, SetStateAction } from 'react';
import { useSettingsState, type SyncSettings } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend } from '../../api';
import { buildAlertSettingsPayload, type SyncSettingsFormState } from './form';

interface FailureAlertPanelProps {
  formState: SyncSettingsFormState;
  setFormState: Dispatch<SetStateAction<SyncSettingsFormState>>;
}

export function FailureAlertPanel({
  formState,
  setFormState,
}: FailureAlertPanelProps) {
  const { dispatch } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();

  const handleSave = async () => {
    try {
      const payload = await apiSend('/api/settings', 'PATCH', buildAlertSettingsPayload(formState));
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
      showToast(t('sync.alertSaved', 'Failure alerts saved.'));
    } catch (err) {
      showToast((err as Error)?.message || t('sync.alertSaveFailed', 'Failed to save failure alerts.'));
    }
  };

  return (
    <div className="panel sync-alerts">
      <div className="panel-header">
        <div>
          <h2>{t('sync.alertTitle', 'Failure Alerts')}</h2>
          <p className="muted">{t('sync.alertSubtitle', 'Choose whether sync failures should send notifications.')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-sync-alert-save" type="button" onClick={handleSave}>
            {t('sync.save', 'Save')}
          </button>
        </div>
      </div>
      <div className="sync-form-sections">
        <section className="sync-form-section">
          <div className="sync-section-title">{t('sync.alertSection', 'Delivery')}</div>
          <div className="form-grid">
            <label className="switch">
              <span>{t('sync.alertEnabled', 'Enable failure email alerts')}</span>
              <input
                type="checkbox"
                id="sync-alert-enabled"
                checked={formState.alertEnabled}
                onChange={(event) => setFormState((prev) => ({ ...prev, alertEnabled: event.target.checked }))}
              />
            </label>
            <label>
              <span>{t('sync.alertEmail', 'Alert email')}</span>
              <input
                type="email"
                id="sync-alert-email"
                placeholder="alerts@example.com"
                value={formState.alertEmail}
                onChange={(event) => setFormState((prev) => ({ ...prev, alertEmail: event.target.value }))}
              />
            </label>
          </div>
        </section>
      </div>
    </div>
  );
}
