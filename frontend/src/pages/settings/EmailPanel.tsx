import { useState, type Dispatch, type SetStateAction } from 'react';
import { useSettingsState, type SyncSettings } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend } from '../../api';
import {
  buildEmailPayload,
  buildTestEmailPayload,
  type SyncSettingsFormState,
} from './form';

interface EmailPanelProps {
  formState: SyncSettingsFormState;
  setFormState: Dispatch<SetStateAction<SyncSettingsFormState>>;
}

export function EmailPanel({
  formState,
  setFormState,
}: EmailPanelProps) {
  const { dispatch } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [testing, setTesting] = useState(false);
  const [testRecipient, setTestRecipient] = useState(() => (
    formState.alertEmail || formState.fromEmail || formState.smtpUser
  ));

  const saveEmail = async () => {
    try {
      const payload = await apiSend('/api/settings', 'PATCH', buildEmailPayload(formState));
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
      showToast(t('email.saveSuccess', 'Email settings saved.'));
    } catch (err) {
      showToast((err as Error)?.message || t('email.saveFailed', 'Failed to save email settings.'));
    }
  };

  const testEmail = async () => {
    const toEmail = testRecipient.trim();
    if (!toEmail) {
      showToast(t('email.testRecipientRequired', 'Enter a test recipient email first.'));
      return;
    }
    setTesting(true);
    try {
      const payload = await apiSend(
        '/api/settings/test-email',
        'POST',
        buildTestEmailPayload(formState, toEmail),
        { timeoutMs: 10000 },
      );
      showToast(t('email.testSuccess', 'Test email sent to {email}.').replace('{email}', (payload.to_email as string) || toEmail));
    } catch (err) {
      if ((err as Record<string, unknown>)?.code === 'TIMEOUT') {
        showToast(t('email.testTimeout', 'Timed out while sending test email.'));
      } else {
        showToast((err as Error)?.message || t('email.testFailed', 'Failed to send test email.'));
      }
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="panel sync-email">
      <div className="panel-header">
        <div>
          <h2>{t('email.title', 'Email Settings')}</h2>
          <p className="muted">{t('email.subtitle', 'SMTP config for alert emails.')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-email-save" type="button" onClick={saveEmail}>
            {t('email.save', 'Save')}
          </button>
          <button className="btn ghost" id="btn-email-test" type="button" disabled={testing} onClick={testEmail}>
            <span className={`loading-spinner${testing ? '' : ' is-hidden'}`} id="email-test-spinner" aria-hidden="true"></span>
            <span id="email-test-label">{testing ? t('email.testing', 'Sending...') : t('email.test', 'Test')}</span>
          </button>
        </div>
      </div>
      <div className="sync-form-sections">
        <section className="sync-form-section">
          <div className="sync-section-title">{t('email.sectionSmtp', 'SMTP')}</div>
          <div className="form-grid">
            <label>
              <span>{t('email.smtpHost', 'SMTP host')}</span>
              <input
                type="text"
                id="email-smtp-host"
                placeholder="smtp.example.com"
                value={formState.smtpHost}
                onChange={(event) => setFormState((prev) => ({ ...prev, smtpHost: event.target.value }))}
              />
            </label>
            <label>
              <span>{t('email.smtpPort', 'SMTP port')}</span>
              <input
                type="number"
                id="email-smtp-port"
                min="1"
                placeholder="587"
                value={formState.smtpPort}
                onChange={(event) => setFormState((prev) => ({ ...prev, smtpPort: event.target.value }))}
              />
            </label>
            <label>
              <span>{t('email.smtpUser', 'SMTP user')}</span>
              <input
                type="text"
                id="email-smtp-user"
                placeholder="username@example.com"
                value={formState.smtpUser}
                onChange={(event) => setFormState((prev) => ({ ...prev, smtpUser: event.target.value }))}
              />
            </label>
            <label>
              <span>{t('email.smtpPassword', 'SMTP password')}</span>
              <input
                type="password"
                id="email-smtp-password"
                placeholder="app password"
                value={formState.smtpPassword}
                onChange={(event) => setFormState((prev) => ({ ...prev, smtpPassword: event.target.value }))}
              />
            </label>
            <label>
              <span>{t('email.fromEmail', 'From email')}</span>
              <input
                type="email"
                id="email-from"
                placeholder="no-reply@example.com"
                value={formState.fromEmail}
                onChange={(event) => setFormState((prev) => ({ ...prev, fromEmail: event.target.value }))}
              />
            </label>
            <label className="switch">
              <span>{t('email.tls', 'Enable TLS')}</span>
              <input
                type="checkbox"
                id="email-tls"
                checked={formState.smtpTls}
                onChange={(event) => setFormState((prev) => ({
                  ...prev,
                  smtpTls: event.target.checked,
                  smtpPort: event.target.checked ? '587' : '25',
                }))}
              />
            </label>
          </div>
        </section>
        <section className="sync-form-section">
          <div className="sync-section-title">{t('email.sectionTest', 'Test Delivery')}</div>
          <div className="form-grid">
            <label className="sync-textarea-field">
              <span>{t('email.testRecipient', 'Test recipient')}</span>
              <input
                type="email"
                id="email-test-recipient"
                placeholder="alerts@example.com"
                value={testRecipient}
                onChange={(event) => setTestRecipient(event.target.value)}
              />
              <small className="muted">{t('email.testRecipientHint', 'Used only for test email delivery, not for failure alerts.')}</small>
            </label>
          </div>
        </section>
      </div>
    </div>
  );
}
