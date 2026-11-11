import { useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { emitRefresh } from '../../utils/events';

/** Whether regular readers can discover Caixin and start new subscriptions. */
export function CaixinVisibilityPanel() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [visible, setVisible] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const settings = await apiGet('/api/admin/media') as Record<string, unknown>;
        setVisible(Boolean(settings.caixin_visible_to_users));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const update = async (next: boolean) => {
    setSaving(true);
    try {
      await apiSend('/api/admin/media', 'PATCH', { caixin_visible_to_users: next });
      setVisible(next);
      emitRefresh();
      showToast(next
        ? t('admin.caixin.visibleSaved', '普通用户现在可以发现并新增财新订阅。')
        : t('admin.caixin.hiddenSaved', '普通用户不再看到财新发现入口，已有订阅仍可阅读。'));
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('admin.caixin.userAccess', '普通用户订阅')}</h2>
          <p className="muted">
            {t(
              'admin.caixin.userAccessHint',
              '隐藏发现入口并禁止新增订阅；已有订阅仍可阅读。后台采集不受影响。',
            )}
          </p>
        </div>
      </div>
      {loading ? (
        <p className="muted">{t('admin.loading', '加载中…')}</p>
      ) : (
        <label className="toolbar">
          <input
            type="checkbox"
            checked={visible}
            disabled={saving}
            onChange={(event) => void update(event.target.checked)}
          />
          <span>{t('admin.caixin.visibleToUsers', '允许普通用户发现并新增财新订阅')}</span>
        </label>
      )}
    </div>
  );
}
