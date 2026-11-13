import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AccountSyncStatus } from '../../components/AccountSyncStatus';
import { AccountSyncFailures } from './AccountSyncFailures';
import { apiSend, isAuthError } from '../../api';
import { useToast } from '../../hooks/useToast';
import { useI18n } from '../../i18n';
import { useGroupsState } from '../../store/groups';
import type { Account } from '../../store/shared';
import { copyToClipboard } from '../../utils/clipboard';
import { emitRefresh } from '../../utils/events';
import { formatDateTime, formatRelativeTime } from '../../utils/format';
import { getSourceLabel } from '../../utils/source';

interface AccountDetailsModalProps {
  account: Account;
  onClose: () => void;
  onViewArticles: () => void;
}

export function AccountDetailsModal({ account, onClose, onViewArticles }: AccountDetailsModalProps) {
  const { t } = useI18n();
  const { dispatch } = useGroupsState();
  const { showToast } = useToast();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const intervalId = useId();
  const [saving, setSaving] = useState(false);
  const source = account.source || 'weixin';
  const published = formatRelativeTime(account.latest_publish_at, t);
  const checkedAt = account.sync?.last_checked_at || account.last_synced_at;
  const checked = formatRelativeTime(checkedAt, t);

  useEffect(() => {
    const dialog = dialogRef.current!;
    const opener = document.activeElement;
    const root = document.documentElement;
    const { overflow, scrollbarGutter } = root.style;
    if (window.innerWidth > root.clientWidth) root.style.scrollbarGutter = 'stable';
    root.style.overflow = 'hidden';
    dialog.showModal();
    return () => {
      dialog.close();
      root.style.overflow = overflow;
      root.style.scrollbarGutter = scrollbarGutter;
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, []);

  const toggleDisabled = async () => {
    if (saving) return;
    const isDisabled = !account.is_disabled;
    setSaving(true);
    try {
      await apiSend(`/api/account/${encodeURIComponent(account.biz)}`, 'PATCH', { is_disabled: isDisabled });
      dispatch({ type: 'UPDATE_ACCOUNT', biz: account.biz, patch: { is_disabled: isDisabled } });
      emitRefresh();
      showToast(isDisabled ? t('accounts.syncDisabledToast', 'Sync disabled.') : t('accounts.syncEnabledToast', 'Sync enabled.'));
    } catch (err) {
      if (!isAuthError(err)) showToast(t('accounts.syncToggleFailed', 'Failed to update sync status.'));
    } finally {
      setSaving(false);
    }
  };

  const saveInterval = async (value: string) => {
    if (saving) return;
    const interval = value === '' ? null : Number(value);
    setSaving(true);
    try {
      await apiSend(`/api/account/${encodeURIComponent(account.biz)}`, 'PATCH', { sync_interval_days: interval });
      dispatch({ type: 'UPDATE_ACCOUNT', biz: account.biz, patch: { sync_interval_days: interval } });
      showToast(t('accounts.syncSaved', 'Sync strategy updated.'));
    } catch (err) {
      if (!isAuthError(err)) showToast(t('accounts.syncFailed', 'Failed to update sync strategy.'));
    } finally {
      setSaving(false);
    }
  };

  const copyIdentifier = async () => {
    try {
      await copyToClipboard(account.biz);
      showToast(t('accounts.identifierCopied', '账号标识已复制'));
    } catch {
      showToast(t('articles.copyFailed', '复制失败'));
    }
  };

  return createPortal(
    <dialog
      ref={dialogRef}
      className="account-dialog"
      aria-labelledby={titleId}
      aria-modal="true"
      onCancel={onClose}
      onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}
    >
      <header className="account-dialog-header">
        <img className={`account-avatar${source === 'weixin' ? ' is-weixin' : ''}`} src={account.avatar_url} alt="" onError={(event) => { event.currentTarget.style.visibility = 'hidden'; }} />
        <div className="account-dialog-heading">
          <h2 id={titleId}>{account.nickname}</h2>
          <p className="account-sub">{getSourceLabel(source, t)}{account.alias ? ` · ${account.alias}` : ''}</p>
        </div>
        <button className="account-dialog-close icon-btn" type="button" aria-label={t('common.close', '关闭')} onClick={onClose}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
      </header>
      <div className="account-dialog-body">
        <dl className="account-dialog-stats">
          <div><dt>{t('accounts.collected', '已收录文章')}</dt><dd>{account.article_count.toLocaleString('zh-CN')}<small>{t('accounts.articleUnit', '篇')}</small></dd></div>
          <div><dt>{t('accounts.latestArticleLabel', '最新文章')}</dt><dd><time dateTime={account.latest_publish_at || undefined}>{published || '—'}</time>{published && <small className="account-dialog-date">{formatDateTime(account.latest_publish_at)}</small>}</dd></div>
        </dl>
        <section className="account-dialog-section">
          <h3>{t('accounts.syncDetails', '同步详情')}</h3>
          <dl className="account-dialog-rows">
            <div><dt>{t('accounts.subscriptionSync', '订阅同步')}</dt><dd>{account.is_disabled ? t('accounts.paused', '已暂停') : t('accounts.enabled', '已启用')}</dd></div>
            <div><dt>{t('accounts.lastCheck', '最近检查')}</dt><dd><time dateTime={checkedAt || undefined}>{checked || t('accounts.lastSyncedEmpty', 'Not synced yet')}</time>{checked && <small className="account-dialog-date">{formatDateTime(checkedAt)}</small>}</dd></div>
          </dl>
          <div className="account-dialog-status">
            {account.sync && <AccountSyncStatus sync={account.sync} biz={account.biz} disabled={account.is_disabled} />}
            {!account.sync && (account.backfill_running || account.backfill_pending) && <p className="account-sub">{account.backfill_running ? t('accounts.syncing', 'Fetching article history') : t('accounts.syncQueued', 'Article history queued')}</p>}
          </div>
        </section>
        <AccountSyncFailures biz={account.biz} onClose={onClose} />
        <section className="account-dialog-section">
          <h3>{t('accounts.syncSettings', '同步设置')}</h3>
          {source === 'weixin'
            ? <p className="account-sub account-dialog-hint">{t('accounts.weixinUpdates', '通过实时推送更新；可手动检查近期文章。')}</p>
            : source === 'caixin'
              ? <p className="account-sub account-dialog-hint">{t('accounts.caixinIntervalGlobal', '财新所有板块共用一个小时级同步间隔。')}</p>
            : <div className="account-dialog-interval">
              <label htmlFor={intervalId}>{t('accounts.syncInterval', 'Sync interval')}</label>
              <select id={intervalId} value={account.sync_interval_days === null ? '' : String(account.sync_interval_days)} disabled={saving} onChange={(event) => void saveInterval(event.target.value)}>
                <option value="">{t('accounts.syncIntervalAuto', '跟随来源设置')}</option>
                <option value="1">{t('accounts.syncInterval1', '每天一次')}</option>
                <option value="3">{t('accounts.syncInterval3', 'Every 3 days')}</option>
                <option value="7">{t('accounts.syncInterval7', 'Weekly')}</option>
                <option value="14">{t('accounts.syncInterval14', 'Biweekly')}</option>
                <option value="30">{t('accounts.syncInterval30', 'Monthly')}</option>
              </select>
            </div>}
        </section>
        <div className="account-dialog-identifier">
          <div><span>{source === 'weixin' ? t('accounts.wechatBiz', '公众号 biz') : t('accounts.identifier', '账号标识')}</span><code>{account.biz}</code></div>
          <button className="btn ghost" type="button" onClick={() => void copyIdentifier()}>{t('accounts.copyIdentifier', '复制标识')}</button>
        </div>
      </div>
      <footer className="account-dialog-footer">
        <button className="btn ghost" type="button" onClick={() => { onClose(); onViewArticles(); }}>{t('accounts.viewArticles', '查看文章')}</button>
        <button className="btn" type="button" disabled={saving} onClick={() => void toggleDisabled()}>
          {account.is_disabled ? t('accounts.resumeSync', '恢复同步') : t('accounts.pauseSync', '暂停同步')}
        </button>
      </footer>
    </dialog>,
    document.body,
  );
}
