import { useCallback, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { getSourceLabel } from '../../utils/source';
import { LoginPanel } from './LoginPanel';
import { SourceCredentialPanel } from './SourceCredentialPanel';
import { SourceProxyPanel } from './SourceProxyPanel';
import { SourceSubscriptionPanel } from './SourceSubscriptionPanel';
import { SourceSchedulePanel } from './SourceSchedulePanel';
import { AdminSyncSection } from './AdminSyncSection';
import { CaixinVisibilityPanel } from './CaixinVisibilityPanel';
import { SyncHealthPanel } from './SyncHealthPanel';

/**
 * One source, everything about it: how hippo signs in, what it crawls, and what
 * that source's sync is doing. The WeChat branch is the daemon's session; the
 * media branches own a credential and a proxy.
 */
export function SourceDetailPage() {
  const { source = '' } = useParams();
  const { t } = useI18n();
  const navigate = useNavigate();
  const [caixinCollection, setCaixinCollection] = useState({ source: '', enabled: false });
  const onCaixinCollectionChange = useCallback((enabled: boolean) => {
    setCaixinCollection({ source, enabled });
  }, [source]);
  const caixinHasEnabledChannels = caixinCollection.source === source && caixinCollection.enabled;

  if (!source) {
    return <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>;
  }

  return (
    <div className="admin-page">
      <div className="admin-page-head">
        <button className="btn ghost" type="button" onClick={() => navigate('/admin/source')}>
          ← {t('admin.source.back', '全部来源')}
        </button>
        <h2 className="panel-title">{getSourceLabel(source, t)}</h2>
      </div>

      {source === 'weixin' ? (
        <>
          <LoginPanel />
          <SyncHealthPanel showPush />
        </>
      ) : (
        <SourceCredentialPanel source={source} />
      )}

      {source === 'caixin' ? (
        <section className="panel">
          <h2>{t('admin.caixin.proxyTitle', '网络分流')}</h2>
          <p>
            {t(
              'admin.caixin.proxyRoute',
              'gateway.caixin.com 和 deepview.caixin.com 走公司代理，其他财新域名直连。',
            )}
          </p>
          <p className="muted">
            {t(
              'admin.caixin.proxyManaged',
              '这两条路由由实例的财新代理配置文件统一管理，这里不需要重复填写代理。',
            )}
          </p>
        </section>
      ) : (
        <SourceProxyPanel source={source} />
      )}

      {source === 'weixin' ? (
        <p className="muted">{t('sync.weixinPushHint', '微信公众号通过实时推送更新，可手动检查近期文章。')}</p>
      ) : (
        <SourceSchedulePanel key={source} source={source} />
      )}

      <SourceSubscriptionPanel
        source={source}
        onCollectionStateChange={source === 'caixin' ? onCaixinCollectionChange : undefined}
      />

      {source === 'caixin' ? <CaixinVisibilityPanel /> : null}

      <AdminSyncSection
        source={source}
        disabled={source === 'caixin' && !caixinHasEnabledChannels}
        disabledReason={source === 'caixin' && !caixinHasEnabledChannels
          ? t('admin.caixin.noEnabledChannels', '尚未启用任何采集板块。')
          : ''}
      />
    </div>
  );
}
