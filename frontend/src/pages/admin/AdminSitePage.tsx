import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';

interface SiteSettings {
  registration_enabled: boolean;
  site_name: string;
  public_base_url: string;
}

const EMPTY: SiteSettings = { registration_enabled: false, site_name: 'Hippo', public_base_url: '' };

export function AdminSitePage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [settings, setSettings] = useState<SiteSettings>(EMPTY);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const payload = await apiGet('/api/admin/site');
      setSettings({
        registration_enabled: Boolean(payload.registration_enabled),
        site_name: String(payload.site_name ?? 'Hippo'),
        public_base_url: String(payload.public_base_url ?? ''),
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (updates: Partial<SiteSettings>) => {
    try {
      const payload = await apiSend('/api/admin/site', 'PATCH', updates);
      setSettings({
        registration_enabled: Boolean(payload.registration_enabled),
        site_name: String(payload.site_name ?? 'Hippo'),
        public_base_url: String(payload.public_base_url ?? ''),
      });
      showToast(t('admin.site.saved', '站点设置已保存'));
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  if (loading) {
    return <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>;
  }

  return (
    <div className="admin-page">
      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.site.title', '站点设置')}</h2>
        </div>
        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.site.name', '站点名称')}</span>
            <input
              className="input"
              value={settings.site_name}
              onChange={(event) => setSettings({ ...settings, site_name: event.target.value })}
              onBlur={() => void save({ site_name: settings.site_name })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.site.baseUrl', 'Public Base URL')}</span>
            <input
              className="input"
              placeholder="https://hippo.example.com"
              value={settings.public_base_url}
              onChange={(event) => setSettings({ ...settings, public_base_url: event.target.value })}
              onBlur={() => void save({ public_base_url: settings.public_base_url })}
            />
          </label>
        </div>
        <p className="muted admin-note">
          {t(
            'admin.site.baseUrlHint',
            '用于拼接验证与重置邮件里的链接；留空时按请求 Host 推断。',
          )}
        </p>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.site.registration', '注册')}</h2>
        </div>
        <label className="auth-check">
          <input
            type="checkbox"
            checked={settings.registration_enabled}
            onChange={(event) => void save({ registration_enabled: event.target.checked })}
          />
          <span>{t('admin.site.registrationEnabled', '开放注册')}</span>
        </label>
        <p className="muted admin-note">
          {t(
            'admin.site.registrationHint',
            '关闭后新账号无法注册（接口返回 403），已有账号不受影响。',
          )}
        </p>
      </section>
    </div>
  );
}
