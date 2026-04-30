import { useState, useEffect } from 'react';
import { useSettingsState, type SyncSettings } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend } from '../../api';

export function EmailPanel() {
  const { state, dispatch } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const [collapsed, setCollapsed] = useState(true);
  const [testing, setTesting] = useState(false);
  const isNarrow = window.matchMedia('(max-width: 760px)').matches;

  const settings = state.syncSettings;
  const email = settings?.email || {};

  useEffect(() => {
    if (!email) return;
    const host = document.getElementById('email-smtp-host') as HTMLInputElement | null;
    const user = document.getElementById('email-smtp-user') as HTMLInputElement | null;
    const password = document.getElementById('email-smtp-password') as HTMLInputElement | null;
    const from = document.getElementById('email-from') as HTMLInputElement | null;
    const tls = document.getElementById('email-tls') as HTMLInputElement | null;
    const port = document.getElementById('email-smtp-port') as HTMLInputElement | null;

    if (host) host.value = (email.smtp_host as string) || '';
    if (user) user.value = (email.smtp_user as string) || '';
    if (password) password.value = (email.smtp_password as string) || '';
    if (from) from.value = (email.from_email as string) || '';
    const tlsEnabled = email.smtp_tls !== false;
    if (tls) tls.checked = tlsEnabled;
    if (port) port.value = String(email.smtp_port ?? (tlsEnabled ? 587 : 25));
  }, [email]);

  const saveEmail = async () => {
    const host = (document.getElementById('email-smtp-host') as HTMLInputElement)?.value.trim();
    const port = Number((document.getElementById('email-smtp-port') as HTMLInputElement)?.value);
    const user = (document.getElementById('email-smtp-user') as HTMLInputElement)?.value.trim();
    const password = (document.getElementById('email-smtp-password') as HTMLInputElement)?.value;
    const tls = (document.getElementById('email-tls') as HTMLInputElement)?.checked;
    const from = (document.getElementById('email-from') as HTMLInputElement)?.value.trim();
    const alertEnabled = (document.getElementById('sync-alert-enabled') as HTMLInputElement)?.checked;
    const alertEmail = (document.getElementById('sync-alert-email') as HTMLInputElement)?.value.trim();

    try {
      const body: Record<string, unknown> = {
        alert_enabled: alertEnabled,
        alert_email: alertEmail,
        email: { smtp_host: host, smtp_port: port, smtp_user: user, smtp_password: password, smtp_tls: tls, from_email: from },
      };
      const payload = await apiSend('/api/settings', 'PATCH', body);
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
      showToast(t('email.saveSuccess', 'Email settings saved.'));
    } catch (err) {
      showToast((err as Error)?.message || t('email.saveFailed', 'Failed to save email settings.'));
    }
  };

  const testEmail = async () => {
    setTesting(true);
    try {
      const host = (document.getElementById('email-smtp-host') as HTMLInputElement)?.value.trim();
      const port = Number((document.getElementById('email-smtp-port') as HTMLInputElement)?.value);
      const userEl = (document.getElementById('email-smtp-user') as HTMLInputElement)?.value.trim();
      const password = (document.getElementById('email-smtp-password') as HTMLInputElement)?.value;
      const tls = (document.getElementById('email-tls') as HTMLInputElement)?.checked;
      const from = (document.getElementById('email-from') as HTMLInputElement)?.value.trim();
      const toEmail = (document.getElementById('sync-alert-email') as HTMLInputElement)?.value.trim();

      const payload = await apiSend('/api/settings/test-email', 'POST', {
        to_email: toEmail,
        email: { smtp_host: host, smtp_port: port, smtp_user: userEl, smtp_password: password, smtp_tls: tls, from_email: from },
      }, { timeoutMs: 10000 });
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
          <div className="section-kicker">{t('email.settingsKicker', 'Alerts')}</div>
          <h2>{t('email.title', 'Email Settings')}</h2>
          <p className="muted">{t('email.subtitle', 'SMTP config for alert emails.')}</p>
        </div>
        <div className="toolbar">
          {isNarrow && (
            <button
              className="btn ghost sync-mobile-toggle"
              id="btn-sync-email-toggle"
              type="button"
              onClick={() => setCollapsed(!collapsed)}
              aria-expanded={!collapsed}
            >
              {collapsed ? t('email.showSettings', 'Show settings') : t('email.hideSettings', 'Hide settings')}
            </button>
          )}
          <button className="btn" id="btn-email-save" type="button" onClick={saveEmail}>
            {t('email.save', 'Save')}
          </button>
          <button className="btn ghost" id="btn-email-test" type="button" disabled={testing} onClick={testEmail}>
            <span className={`loading-spinner${testing ? '' : ' is-hidden'}`} id="email-test-spinner" aria-hidden="true"></span>
            <span id="email-test-label">{testing ? t('email.testing', 'Sending...') : t('email.test', 'Test')}</span>
          </button>
        </div>
      </div>
      <div className={`sync-form-sections${isNarrow && collapsed ? ' is-mobile-collapsed' : ''}`}>
        <section className="sync-form-section">
          <div className="sync-section-title">{t('email.sectionTarget', 'Alert Target')}</div>
          <div className="form-grid">
            <label className="switch">
              <span>{t('sync.alertEnabled', 'Enable alert email')}</span>
              <input type="checkbox" id="sync-alert-enabled" />
            </label>
            <label>
              <span>{t('sync.alertEmail', 'Alert email')}</span>
              <input type="email" id="sync-alert-email" placeholder="alerts@example.com" />
            </label>
          </div>
        </section>
        <section className="sync-form-section">
          <div className="sync-section-title">{t('email.sectionSmtp', 'SMTP')}</div>
          <div className="form-grid">
            <label>
              <span>{t('email.smtpHost', 'SMTP host')}</span>
              <input type="text" id="email-smtp-host" placeholder="smtp.example.com" />
            </label>
            <label>
              <span>{t('email.smtpPort', 'SMTP port')}</span>
              <input type="number" id="email-smtp-port" min="1" placeholder="587" />
            </label>
            <label>
              <span>{t('email.smtpUser', 'SMTP user')}</span>
              <input type="text" id="email-smtp-user" placeholder="username@example.com" />
            </label>
            <label>
              <span>{t('email.smtpPassword', 'SMTP password')}</span>
              <input type="password" id="email-smtp-password" placeholder="app password" />
            </label>
            <label>
              <span>{t('email.fromEmail', 'From email')}</span>
              <input type="email" id="email-from" placeholder="no-reply@example.com" />
            </label>
            <label className="switch">
              <span>{t('email.tls', 'Enable TLS')}</span>
              <input type="checkbox" id="email-tls" onChange={(e) => {
                const port = document.getElementById('email-smtp-port') as HTMLInputElement | null;
                if (port) port.value = e.target.checked ? '587' : '25';
              }} />
            </label>
          </div>
        </section>
      </div>
    </div>
  );
}
