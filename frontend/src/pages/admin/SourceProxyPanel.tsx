import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { ProxyFields } from './ProxyFields';

/** Sources that open their own connections, and so each pick their own exit. */
const PROXY_SOURCES = ['weixin', 'bilibili', 'youtube', 'podcast'];

/**
 * One source's proxy, and the instance default it may follow.
 *
 * The default is shown here as well because a source left on "follow the default"
 * is only meaningful next to the value it follows — and the exit IP matters per
 * source (Bilibili blacklists the proxy, YouTube needs it).
 */
export function SourceProxyPanel({ source }: { source: string }) {
  const { t } = useI18n();
  const [proxy, setProxy] = useState('');
  const [defaultProxy, setDefaultProxy] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const payload = await apiGet('/api/admin/media');
      const record = payload as Record<string, string>;
      setProxy(String(record[`proxy_${source}`] ?? ''));
      setDefaultProxy(String(record.proxy_default ?? ''));
    } finally {
      setLoading(false);
    }
  }, [source]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (patch: Record<string, string>) => {
    const payload = await apiSend('/api/admin/media', 'PATCH', patch);
    const record = payload as Record<string, string>;
    setProxy(String(record[`proxy_${source}`] ?? ''));
    setDefaultProxy(String(record.proxy_default ?? ''));
  };

  if (!PROXY_SOURCES.includes(source)) {
    return null;
  }

  if (loading) {
    return <div className="panel muted">{t('admin.loading', '加载中…')}</div>;
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('admin.media.proxy', '代理')}</h2>
          <p className="muted">
            {t('admin.source.proxySubtitle', '这个来源的出口；每个来源的出口可以不同。')}
          </p>
        </div>
      </div>
      <div className="form-grid">
        <ProxyFields
          values={{ proxy_default: defaultProxy, [`proxy_${source}`]: proxy }}
          onSave={save}
          sources={[source]}
        />
      </div>
    </div>
  );
}
