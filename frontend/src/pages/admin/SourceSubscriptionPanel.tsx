import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { formatRelativeTime } from '../../utils/format';
import { emitRefresh } from '../../utils/events';

interface InstanceChannel {
  biz: string;
  nickname: string;
  source: string;
  alias: string | null;
  is_disabled: boolean;
  last_synced_at: string | null;
  latest_publish_at: string | null;
  article_count: number;
  subscribers: number;
}

/**
 * The shared catalogue for one source, not the signed-in administrator's own
 * subscriptions. Caixin rows can also change instance collection eligibility;
 * other sources retain their existing subscription-driven collection policy.
 */
export function SourceSubscriptionPanel({
  source,
  onCollectionStateChange,
}: {
  source: string;
  onCollectionStateChange?: (hasEnabledChannels: boolean) => void;
}) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [channels, setChannels] = useState<InstanceChannel[]>([]);
  const [draftDisabled, setDraftDisabled] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const requestIdRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setError('');
    try {
      const payload = await apiGet(`/api/admin/source/${encodeURIComponent(source)}/channel`);
      if (requestId !== requestIdRef.current) return;
      const next = (payload.channels || []) as InstanceChannel[];
      setChannels(next);
      setDraftDisabled(new Set(next.filter((item) => item.is_disabled).map((item) => item.biz)));
      onCollectionStateChange?.(next.some((item) => !item.is_disabled));
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
      onCollectionStateChange?.(false);
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [onCollectionStateChange, source]);

  useEffect(() => {
    queueMicrotask(() => {
      void load();
    });
  }, [load]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return channels;
    return channels.filter((item) =>
      [item.nickname, item.alias, item.biz].some((value) => value?.toLocaleLowerCase().includes(needle)),
    );
  }, [channels, query]);

  const dirty = channels.some((item) => draftDisabled.has(item.biz) !== item.is_disabled);
  const enabled = channels.filter((item) => !draftDisabled.has(item.biz)).length;

  const toggle = (biz: string) => {
    setDraftDisabled((current) => {
      const next = new Set(current);
      if (next.has(biz)) next.delete(biz);
      else next.add(biz);
      return next;
    });
  };

  const save = async () => {
    if (!dirty || saving) return;
    const enabledBiz = channels
      .filter((item) => item.is_disabled && !draftDisabled.has(item.biz))
      .map((item) => item.biz);
    const disabledBiz = channels
      .filter((item) => !item.is_disabled && draftDisabled.has(item.biz))
      .map((item) => item.biz);
    setSaving(true);
    try {
      if (enabledBiz.length) {
        await apiSend(`/api/admin/source/${encodeURIComponent(source)}/channel`, 'PATCH', {
          biz_list: enabledBiz,
          is_disabled: false,
        });
      }
      if (disabledBiz.length) {
        await apiSend(`/api/admin/source/${encodeURIComponent(source)}/channel`, 'PATCH', {
          biz_list: disabledBiz,
          is_disabled: true,
        });
      }
      await load();
      emitRefresh();
      showToast(t('admin.source.channelsSaved', '采集范围已保存。'));
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err));
      await load();
    } finally {
      setSaving(false);
    }
  };

  const editable = source === 'caixin';

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{editable ? t('admin.source.collectionScope', '采集板块') : t('admin.source.catalog', '实例采集目录')}</h2>
          <p className="muted">
            {editable
              ? t('admin.source.collectionScopeHint', '这里控制实例采集范围，与管理员个人订阅无关。')
              : t('admin.source.catalogHint', '这里展示全实例目录；管理员个人订阅不会改变后台采集。')}
          </p>
        </div>
        <div className="toolbar">
          <input
            type="search"
            className="subscription-search"
            aria-label={t('admin.source.searchChannels', '搜索采集对象')}
            placeholder={t('admin.source.searchChannels', '搜索采集对象')}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {editable ? (
            <button className="btn" type="button" disabled={!dirty || saving} onClick={() => void save()}>
              {saving ? t('common.saving', '正在保存…') : t('admin.source.saveChannels', '保存范围')}
            </button>
          ) : null}
        </div>
      </div>

      {editable ? (
        <p className="muted admin-note">
          {t('admin.source.enabledCount', '已启用 {enabled} / {total} 个板块')
            .replace('{enabled}', String(enabled))
            .replace('{total}', String(channels.length))}
        </p>
      ) : null}

      {loading ? (
        <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>
      ) : error ? (
        <div className="muted admin-empty">
          {t('admin.source.channelsFailed', '加载实例采集目录失败。')}
          <button className="btn ghost" type="button" onClick={() => void load()}>{t('common.retry', '重试')}</button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="muted admin-empty">{t('admin.source.noChannels', '没有匹配的采集对象。')}</div>
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                {editable ? <th>{t('admin.source.collecting', '采集')}</th> : null}
                <th>{t('groups.title', '名称')}</th>
                <th>{t('admin.source.subscribers', '订阅用户')}</th>
                <th>{t('accounts.articleCountLabel', '已收录文章')}</th>
                <th>{t('accounts.lastUpdatedLabel', '最新文章')}</th>
                <th>{t('accounts.lastSyncedLabel', '最近成功')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filtered.map((channel) => {
                const collecting = !draftDisabled.has(channel.biz);
                return (
                  <tr key={channel.biz} className={collecting ? '' : 'is-disabled'}>
                    {editable ? (
                      <td>
                        <input
                          type="checkbox"
                          aria-label={t('admin.source.collectChannel', '采集 {name}')
                            .replace('{name}', channel.nickname)}
                          checked={collecting}
                          disabled={saving}
                          onChange={() => toggle(channel.biz)}
                        />
                      </td>
                    ) : null}
                    <td>
                      {channel.nickname}
                      <div className="account-sub admin-mono">{channel.biz}</div>
                    </td>
                    <td>{channel.subscribers}</td>
                    <td>{channel.article_count}</td>
                    <td className="muted">{formatRelativeTime(channel.latest_publish_at, t) || '—'}</td>
                    <td className="muted">{formatRelativeTime(channel.last_synced_at, t) || '—'}</td>
                    <td>
                      <Link className="btn ghost" to={`/articles?account=${encodeURIComponent(channel.biz)}`}>
                        {t('groups.viewArticles', '查看文章')}
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
