import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { buildSyncSettingsFormState, type SyncSettingsFormState } from './form';
import { EmailPanel } from './EmailPanel';

const apiSendMock = vi.fn();
const dispatchMock = vi.fn();
const showToastMock = vi.fn();

vi.mock('../../api', () => ({
  apiSend: (...args: unknown[]) => apiSendMock(...args),
  isAuthError: () => false,
}));

vi.mock('../../store/settings', () => ({
  useSettingsState: () => ({ dispatch: dispatchMock }),
}));

vi.mock('../../hooks/useToast', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

const settingsWithStoredPassword = buildSyncSettingsFormState({
  enabled: true,
  interval_minutes: 60,
  sleep_seconds: 15,
  skip_minutes: 30,
  download_content: true,
  download_images: true,
  article_exclude_keywords: '',
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
});

function Harness({ initial }: { initial: SyncSettingsFormState }) {
  const [formState, setFormState] = useState(initial);
  return <EmailPanel formState={formState} setFormState={setFormState} />;
}

const renderPanel = (initial: SyncSettingsFormState = settingsWithStoredPassword) => render(
  <I18nProvider>
    <Harness initial={initial} />
  </I18nProvider>,
);

const passwordInput = () => document.querySelector('#email-smtp-password') as HTMLInputElement;
const testRecipient = () => document.querySelector('#email-test-recipient') as HTMLInputElement;
const saveButton = () => document.querySelector('#btn-email-save') as HTMLButtonElement;
const clearCheckbox = () => document.querySelector('#email-smtp-password-clear') as HTMLInputElement;

describe('EmailPanel', () => {
  beforeEach(() => {
    apiSendMock.mockReset();
    dispatchMock.mockReset();
    showToastMock.mockReset();
    apiSendMock.mockResolvedValue(settingsWithStoredPassword);
  });

  it('never pre-fills the password input with the stored mask', () => {
    renderPanel();
    expect(passwordInput().value).toBe('');
    expect(screen.getByText('已保存密码：留空则继续使用，输入新值则替换。')).toBeTruthy();
  });

  it('keeps the stored secret when the field is left blank', async () => {
    renderPanel();
    fireEvent.click(saveButton());

    await waitFor(() => expect(apiSendMock).toHaveBeenCalled());
    const [, method, body] = apiSendMock.mock.calls[0] as [string, string, { email: Record<string, unknown> }];
    expect(method).toBe('PATCH');
    expect('smtp_password' in body.email).toBe(false);
  });

  it('sends a newly typed secret verbatim', async () => {
    renderPanel();
    fireEvent.change(passwordInput(), { target: { value: '  new secret  ' } });
    fireEvent.click(saveButton());

    await waitFor(() => expect(apiSendMock).toHaveBeenCalled());
    const [, , body] = apiSendMock.mock.calls[0] as [string, string, { email: Record<string, unknown> }];
    expect(body.email.smtp_password).toBe('  new secret  ');
  });

  it('clears the stored secret only through the explicit control', async () => {
    renderPanel();
    expect(clearCheckbox()).toBeTruthy();

    fireEvent.click(clearCheckbox());
    fireEvent.click(saveButton());

    await waitFor(() => expect(apiSendMock).toHaveBeenCalled());
    const [, , body] = apiSendMock.mock.calls[0] as [string, string, { email: Record<string, unknown> }];
    expect(body.email.smtp_password).toBe('');
  });

  it('resets the clear control and the field after a successful save', async () => {
    renderPanel();
    fireEvent.change(passwordInput(), { target: { value: 'typed' } });
    fireEvent.click(clearCheckbox());
    fireEvent.click(saveButton());

    await waitFor(() => expect(showToastMock).toHaveBeenCalled());
    expect(passwordInput().value).toBe('');
    expect(clearCheckbox().checked).toBe(false);
  });

  it('does not clear the stored secret when a test email leaves the field blank', async () => {
    renderPanel();
    fireEvent.change(testRecipient(), { target: { value: 'alerts@example.com' } });
    fireEvent.click(document.querySelector('#btn-email-test')!);

    await waitFor(() => expect(apiSendMock).toHaveBeenCalled());
    const [path, method, body] = apiSendMock.mock.calls[0] as [string, string, { to_email: string; email: Record<string, unknown> }];
    expect(path).toBe('/api/settings/test-email');
    expect(method).toBe('POST');
    expect(body.to_email).toBe('alerts@example.com');
    expect('smtp_password' in body.email).toBe(false);
  });
});
