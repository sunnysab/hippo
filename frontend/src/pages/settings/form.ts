import type { SyncSettings } from '../../store/settings';

export interface SyncSettingsFormState {
  enabled: boolean;
  intervalMinutes: string;
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
  /**
   * A secret the operator just typed. It never carries the stored value or the
   * server's mask: a blank field means "keep what is stored".
   */
  smtpPassword: string;
  /** Whether the server stores a secret, which drives the configured hint and the clear control. */
  smtpPasswordSet?: boolean;
  /** The operator asked to clear the stored secret on the next save. */
  smtpPasswordClear?: boolean;
  smtpTls: boolean;
  fromEmail: string;
}

export interface EmailSettingsPayload {
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  /** Omitted to keep the stored secret; empty to clear it; otherwise the new secret. */
  smtp_password?: string;
  smtp_tls: boolean;
  from_email: string;
}

export type EmailPayload = {
  email: EmailSettingsPayload;
};

export type TestEmailPayload = {
  to_email: string;
  email: EmailSettingsPayload;
};

const toStringValue = (value: unknown, fallback: string): string => {
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
};

export const buildSyncSettingsFormState = (
  settings: SyncSettings | null,
): SyncSettingsFormState => {
  const email = settings?.email;
  const smtpTls = email?.smtp_tls !== false;

  return {
    enabled: Boolean(settings?.enabled),
    intervalMinutes: toStringValue(settings?.interval_minutes, '60'),
    sleepSeconds: toStringValue(settings?.sleep_seconds, '15'),
    skipMinutes: toStringValue(settings?.skip_minutes, '30'),
    downloadContent: Boolean(settings?.download_content),
    downloadImages: Boolean(settings?.download_images),
    articleExcludeKeywords: settings?.article_exclude_keywords || '',
    alertEnabled: Boolean(settings?.alert_enabled),
    alertEmail: settings?.alert_email || '',
    smtpHost: toStringValue(email?.smtp_host, ''),
    smtpPort: toStringValue(email?.smtp_port, smtpTls ? '587' : '25'),
    smtpUser: toStringValue(email?.smtp_user, ''),
    // The response carries a mask, so the field starts empty; a blank field keeps
    // the stored secret until the operator types a new one or asks to clear it.
    smtpPassword: '',
    smtpPasswordSet: Boolean(email?.smtp_password_set),
    smtpPasswordClear: false,
    smtpTls,
    fromEmail: toStringValue(email?.from_email, ''),
  };
};

const toNumber = (value: string, fallback: number): number => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

export const buildSyncSettingsPayload = (form: SyncSettingsFormState): Record<string, unknown> => ({
  enabled: form.enabled,
  interval_minutes: toNumber(form.intervalMinutes, 60),
  sleep_seconds: toNumber(form.sleepSeconds, 15),
  skip_minutes: toNumber(form.skipMinutes, 30),
  download_content: form.downloadContent,
  download_images: form.downloadImages,
});

export const buildFilterSettingsPayload = (form: Pick<SyncSettingsFormState, 'articleExcludeKeywords'>): Record<string, unknown> => ({
  article_exclude_keywords: form.articleExcludeKeywords.trim(),
});

export const buildAlertSettingsPayload = (form: SyncSettingsFormState): Record<string, unknown> => ({
  alert_enabled: form.alertEnabled,
  alert_email: form.alertEmail.trim(),
});

export const buildSmtpPayload = (form: SyncSettingsFormState): EmailSettingsPayload => {
  const email: EmailSettingsPayload = {
    smtp_host: form.smtpHost.trim(),
    smtp_port: toNumber(form.smtpPort, form.smtpTls ? 587 : 25),
    smtp_user: form.smtpUser.trim(),
    smtp_tls: form.smtpTls,
    from_email: form.fromEmail.trim(),
  };
  // A typed secret replaces the stored one and is sent verbatim (a trailing
  // space can be part of a password); an explicit clear sends the empty string;
  // an untouched blank field omits the key so the server keeps its secret.
  if (form.smtpPassword) email.smtp_password = form.smtpPassword;
  else if (form.smtpPasswordClear) email.smtp_password = '';
  return email;
};

export const buildEmailPayload = (form: SyncSettingsFormState): EmailPayload => ({
  email: buildSmtpPayload(form),
});

export const buildTestEmailPayload = (
  form: SyncSettingsFormState,
  toEmail: string,
): TestEmailPayload => ({
  to_email: toEmail.trim(),
  email: buildSmtpPayload(form),
});
