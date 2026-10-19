import { useNavigate, useParams } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { getSourceLabel } from '../../utils/source';
import { LoginPanel } from './LoginPanel';
import { SourceCredentialPanel } from './SourceCredentialPanel';
import { SourceProxyPanel } from './SourceProxyPanel';
import { SourceSubscriptionPanel } from './SourceSubscriptionPanel';
import { AdminSyncSection } from './AdminSyncSection';

/**
 * One source, everything about it: how hippo signs in, what it crawls, and what
 * that source's sync is doing. The WeChat branch is the daemon's session; the
 * media branches own a credential and a proxy.
 */
export function SourceDetailPage() {
  const { source = '' } = useParams();
  const { t } = useI18n();
  const navigate = useNavigate();

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
        <LoginPanel />
      ) : (
        <SourceCredentialPanel source={source} />
      )}

      <SourceProxyPanel source={source} />

      <SourceSubscriptionPanel source={source} />

      <AdminSyncSection source={source} />
    </div>
  );
}
