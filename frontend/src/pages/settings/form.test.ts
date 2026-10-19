import { describe, expect, it } from 'vitest';
import type { SyncSettings } from '../../store/settings';
import {
  buildEmailPayload,
  buildFilterSettingsPayload,
  buildSyncSettingsFormState,
  buildTestEmailPayload,
  type SyncSettingsFormState,
} from './form';

const baseForm = (patch: Partial<SyncSettingsFormState> = {}): SyncSettingsFormState => ({
  enabled: true,
  intervalMinutes: '60',
  sleepSeconds: '15',
  skipMinutes: '30',
  downloadContent: true,
  downloadImages: true,
  articleExcludeKeywords: '',
  alertEnabled: false,
  alertEmail: '',
  smtpHost: 'smtp.example.com',
  smtpPort: '587',
  smtpUser: 'user',
  smtpPassword: '',
  smtpTls: true,
  fromEmail: 'no-reply@example.com',
  ...patch,
});

describe('buildSyncSettingsFormState', () => {
  it('never copies the server mask into the password field', () => {
    const settings: SyncSettings = {
      enabled: true,
      interval_minutes: 60,
      sleep_seconds: 15,
      skip_minutes: 30,
      download_content: true,
      download_images: true,
      article_exclude_keywords: 'promo',
      alert_enabled: false,
      alert_email: '',
      email: {
        smtp_host: 'smtp.example.com',
        smtp_port: 587,
        smtp_user: 'user',
        smtp_password: '••••••',
        smtp_password_set: true,
        smtp_tls: true,
        from_email: 'no-reply@example.com',
      },
    };

    const form = buildSyncSettingsFormState(settings);

    expect(form.smtpPassword).toBe('');
    expect(form.smtpPasswordSet).toBe(true);
    expect(form.smtpPasswordClear).toBe(false);
  });
});

describe('buildFilterSettingsPayload', () => {
  it('sends only the personal reading key, trimmed', () => {
    expect(buildFilterSettingsPayload({ articleExcludeKeywords: '  promo\nad  ' })).toEqual({
      article_exclude_keywords: 'promo\nad',
    });
  });
});

describe('the SMTP password rule', () => {
  it('omits the secret when the field is blank and no clear was asked for', () => {
    const payload = buildEmailPayload(baseForm());
    expect('smtp_password' in payload.email).toBe(false);
  });

  it('sends a typed secret verbatim, without trimming', () => {
    const payload = buildEmailPayload(baseForm({ smtpPassword: '  app pass  ' }));
    expect(payload.email.smtp_password).toBe('  app pass  ');
  });

  it('sends the empty string only for an explicit clear', () => {
    const payload = buildEmailPayload(baseForm({ smtpPasswordClear: true }));
    expect(payload.email.smtp_password).toBe('');
  });

  it('trims the address fields but never the secret', () => {
    const payload = buildEmailPayload(baseForm({ smtpUser: ' user ', fromEmail: ' a@b.c ' }));
    expect(payload.email.smtp_user).toBe('user');
    expect(payload.email.from_email).toBe('a@b.c');
  });

  it('applies the same rule to the test-email payload', () => {
    const blank = buildTestEmailPayload(baseForm(), ' alerts@example.com ');
    expect(blank.to_email).toBe('alerts@example.com');
    expect('smtp_password' in blank.email).toBe(false);

    const typed = buildTestEmailPayload(baseForm({ smtpPassword: 'secret' }), 'a@b.c');
    expect(typed.email.smtp_password).toBe('secret');

    const cleared = buildTestEmailPayload(baseForm({ smtpPasswordClear: true }), 'a@b.c');
    expect(cleared.email.smtp_password).toBe('');
  });
});
