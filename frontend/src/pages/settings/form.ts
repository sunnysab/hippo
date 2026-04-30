import type { SyncSettings } from '../../store/settings';

export interface SyncSettingsFormState {
  enabled: boolean;
  intervalMinutes: string;
  windowStartHour: string;
  windowEndHour: string;
  sleepSeconds: string;
  skipMinutes: string;
  downloadContent: boolean;
  downloadImages: boolean;
  articleExcludeKeywords: string;
  alertEnabled: boolean;
  alertEmail: string;
  smtpHost: string;
  smtpPort: string;
  smtpUser: string;
  smtpPassword: string;
  smtpTls: boolean;
  fromEmail: string;
}

const toStringValue = (value: unknown, fallback: string): string => {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
};

export const buildSyncSettingsFormState = (
  settings: SyncSettings | null,
): SyncSettingsFormState => {
  const email = settings?.email || {};
  const smtpTls = email.smtp_tls !== false;

  return {
    enabled: Boolean(settings?.enabled),
    intervalMinutes: toStringValue(settings?.interval_minutes, '60'),
    windowStartHour: toStringValue(settings?.window_start_hour, '6'),
    windowEndHour: toStringValue(settings?.window_end_hour, '24'),
    sleepSeconds: toStringValue(settings?.sleep_seconds, '0.05'),
    skipMinutes: toStringValue(settings?.skip_minutes, '30'),
    downloadContent: Boolean(settings?.download_content),
    downloadImages: Boolean(settings?.download_images),
    articleExcludeKeywords: settings?.article_exclude_keywords || '',
    alertEnabled: Boolean(settings?.alert_enabled),
    alertEmail: settings?.alert_email || '',
    smtpHost: toStringValue(email.smtp_host, ''),
    smtpPort: toStringValue(email.smtp_port, smtpTls ? '587' : '25'),
    smtpUser: toStringValue(email.smtp_user, ''),
    smtpPassword: toStringValue(email.smtp_password, ''),
    smtpTls,
    fromEmail: toStringValue(email.from_email, ''),
  };
};

const toNumber = (value: string, fallback: number): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

export const buildSyncSettingsPayload = (form: SyncSettingsFormState): Record<string, unknown> => ({
  enabled: form.enabled,
  interval_minutes: toNumber(form.intervalMinutes, 60),
  window_start_hour: toNumber(form.windowStartHour, 6),
  window_end_hour: toNumber(form.windowEndHour, 24),
  sleep_seconds: toNumber(form.sleepSeconds, 0.05),
  skip_minutes: toNumber(form.skipMinutes, 30),
  download_content: form.downloadContent,
  download_images: form.downloadImages,
  article_exclude_keywords: form.articleExcludeKeywords.trim(),
  alert_enabled: form.alertEnabled,
  alert_email: form.alertEmail.trim(),
});

export const buildEmailPayload = (form: SyncSettingsFormState): Record<string, unknown> => ({
  alert_enabled: form.alertEnabled,
  alert_email: form.alertEmail.trim(),
  email: {
    smtp_host: form.smtpHost.trim(),
    smtp_port: toNumber(form.smtpPort, form.smtpTls ? 587 : 25),
    smtp_user: form.smtpUser.trim(),
    smtp_password: form.smtpPassword,
    smtp_tls: form.smtpTls,
    from_email: form.fromEmail.trim(),
  },
});

export const buildTestEmailPayload = (form: SyncSettingsFormState): Record<string, unknown> => ({
  to_email: form.alertEmail.trim(),
  email: {
    smtp_host: form.smtpHost.trim(),
    smtp_port: toNumber(form.smtpPort, form.smtpTls ? 587 : 25),
    smtp_user: form.smtpUser.trim(),
    smtp_password: form.smtpPassword,
    smtp_tls: form.smtpTls,
    from_email: form.fromEmail.trim(),
  },
});
