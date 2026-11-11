import { useEffect, useRef, useState } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import { emitRefresh } from '../../utils/events';
import { getSourceLabel } from '../../utils/source';

interface AddSubscriptionModalProps {
  onClose: () => void;
  /** 目标分组：新订阅直接进当前分组，而不是默认分组。 */
  groupId: number | null;
  /** 订阅成功后把列表刷新一次（新建的频道要立刻出现在分组里）。 */
  onSubscribed?: () => void;
}

interface Preview {
  source: string;
  biz: string;
  nickname: string;
  avatar_url: string | null;
  subscribed: boolean;
}

interface AccountHit {
  biz: string;
  nickname: string;
  alias: string;
  /** 搜索结果里的原始头像地址，添加公众号时要存下来。 */
  round_head_img: string;
  /** 服务端缓存的头像，显示用。 */
  avatar_url: string;
  is_added: boolean;
}

interface CaixinChannel {
  channel_id: number;
  biz: string;
  name: string;
  url: string;
  collecting: boolean;
  has_articles: boolean;
  subscribed: boolean;
  can_subscribe: boolean;
}

/** 输入框里的一行字属于哪一类：一个地址，还是一个要找的名字。 */
type Target = 'link' | 'name';

const SEARCH_DEBOUNCE_MS = 300;

const looksLikeLink = (value: string): boolean => /^https?:\/\//i.test(value);

/**
 * 一个输入框收两种东西：粘贴地址订阅来源，或者打名字搜公众号。
 *
 * 判断只看输入框本身——地址认协议头，其余当名字——所以不需要额外的模式开关，
 * 也不会出现「选错了页签」这种错。
 */
export function AddSubscriptionModal({ onClose, groupId, onSubscribed }: AddSubscriptionModalProps) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [input, setInput] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [hits, setHits] = useState<AccountHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [adding, setAdding] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [caixinChannels, setCaixinChannels] = useState<CaixinChannel[] | null>(null);
  const [caixinOpen, setCaixinOpen] = useState(false);
  const [caixinBusy, setCaixinBusy] = useState('');
  const [caixinLoading, setCaixinLoading] = useState(true);
  const [caixinLoadError, setCaixinLoadError] = useState('');
  const [caixinRetry, setCaixinRetry] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const target: Target = looksLikeLink(input.trim()) ? 'link' : 'name';

  // The parent mounts this dialog only while it is open, so closing unmounts it
  // and the input starts empty next time. Only the pending debounce outlives a
  // close, and it would fire a search nobody is looking at.
  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void apiGet('/api/source/caixin/channel')
      .then((payload) => {
        if (!cancelled) setCaixinChannels((payload.channels || []) as CaixinChannel[]);
      })
      .catch((err: unknown) => {
        if (cancelled || isAuthError(err)) return;
        const apiError = err as { code?: string; message?: string; status?: number };
        setCaixinChannels(null);
        if (apiError.status !== 403 || apiError.code !== 'source.subscription_unavailable') {
          setCaixinLoadError(apiError.message || t('sources.caixinLoadFailed', '加载财新板块失败。'));
        }
      })
      .finally(() => {
        if (!cancelled) setCaixinLoading(false);
      });
    return () => { cancelled = true; };
  }, [caixinRetry, t]);

  useEffect(() => {
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [onClose]);

  const resolve = async () => {
    const url = input.trim();
    if (!url) return;
    setBusy(true);
    setError('');
    setPreview(null);
    try {
      const payload = await apiGet(
        `/api/source/preview?url=${encodeURIComponent(url)}`,
      ) as unknown as Preview;
      setPreview(payload);
    } catch (err) {
      if (isAuthError(err)) return;
      setError((err as Error)?.message || t('sources.resolveFailed', '解析失败。'));
    } finally {
      setBusy(false);
    }
  };

  const search = async (keyword: string) => {
    if (keyword.length < 2) {
      setHits([]);
      return;
    }
    setSearching(true);
    setError('');
    try {
      const payload = await apiGet(
        `/api/account/search?q=${encodeURIComponent(keyword)}&page_size=10`,
      );
      setHits((payload.results || []) as unknown as AccountHit[]);
    } catch (err) {
      if (isAuthError(err)) return;
      setHits([]);
      setError((err as Error)?.message || t('accounts.searchFailed', '搜索失败。'));
    } finally {
      setSearching(false);
    }
  };

  const handleChange = (value: string) => {
    setInput(value);
    setError('');
    setPreview(null);
    if (timerRef.current) clearTimeout(timerRef.current);
    if (looksLikeLink(value.trim())) {
      // 解析要抓远端，不能每敲一个字符打一次：等回车或粘贴完成。
      setHits([]);
      return;
    }
    timerRef.current = setTimeout(() => { void search(value.trim()); }, SEARCH_DEBOUNCE_MS);
  };

  const subscribeSource = async () => {
    setBusy(true);
    try {
      await apiSend('/api/source/channel', 'POST', {
        url: input.trim(),
        ...(groupId != null ? { group_id: groupId } : {}),
      });
      showToast(t('sources.subscribed', '已订阅，稍后会自动同步。'));
      emitRefresh();
      onSubscribed?.();
      onClose();
    } catch (err) {
      if (isAuthError(err)) return;
      setError((err as Error)?.message || t('sources.subscribeFailed', '订阅失败。'));
    } finally {
      setBusy(false);
    }
  };

  const addAccount = async (hit: AccountHit) => {
    setAdding(hit.biz);
    setError('');
    try {
      await apiSend('/api/account', 'POST', {
        biz: hit.biz,
        nickname: hit.nickname,
        alias: hit.alias || null,
        round_head_img: hit.round_head_img || null,
        ...(groupId != null ? { group_id: groupId } : {}),
      });
      setHits((current) => current.map((item) => (
        item.biz === hit.biz ? { ...item, is_added: true } : item
      )));
      showToast(t('accounts.added', '已添加。'));
      emitRefresh();
      onSubscribed?.();
    } catch (err) {
      if (isAuthError(err)) return;
      setError((err as Error)?.message || t('accounts.addFailed', '添加失败。'));
    } finally {
      setAdding('');
    }
  };

  const subscribeCaixin = async (channel: CaixinChannel) => {
    setCaixinBusy(channel.biz);
    setError('');
    try {
      await apiSend('/api/source/channel', 'POST', {
        url: channel.url,
        ...(groupId != null ? { group_id: groupId } : {}),
      });
      setCaixinChannels((current) => current?.map((item) => (
        item.biz === channel.biz ? { ...item, subscribed: true, can_subscribe: false } : item
      )) ?? current);
      showToast(t('sources.caixinSubscribed', '已订阅财新板块，后续更新按实例采集计划进行。'));
      emitRefresh();
      onSubscribed?.();
    } catch (err) {
      if (isAuthError(err)) return;
      setError((err as Error)?.message || t('sources.subscribeFailed', '订阅失败。'));
    } finally {
      setCaixinBusy('');
    }
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Enter') return;
    if (target === 'link') {
      void resolve();
      return;
    }
    const first = hits.find((hit) => !hit.is_added);
    if (first) void addAccount(first);
  };

  return (
    <div className="modal-overlay is-open" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal subscribe-modal">
        <div className="modal-header">
          <div className="modal-title">{t('sources.addTitle', '添加订阅')}</div>
          <button className="icon-btn" type="button" aria-label={t('common.close', '关闭')} onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          {/* 前缀图标跟着判断结果走：输入框里是什么，右边就是什么。 */}
          <div className="input subscribe-input">
            <span className="icon" aria-hidden="true">
              {target === 'link' ? (
                <svg viewBox="0 0 24 24"><path d="M10.6 13.4a1 1 0 0 1 0-1.4l3.5-3.5a3 3 0 0 1 4.2 4.2l-1.8 1.8-1.4-1.4 1.8-1.8a1 1 0 0 0-1.4-1.4L12 12l-.6.6-1.4-1.4.6-.6zm2.8-2.8a1 1 0 0 1 0 1.4l-3.5 3.5a3 3 0 0 1-4.2-4.2l1.8-1.8 1.4 1.4-1.8 1.8a1 1 0 0 0 1.4 1.4L12 12l.6-.6 1.4 1.4-.6.6z"/></svg>
              ) : (
                <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6 6 0 1 0 14 15.5l.27.28v.79L20 21.5 21.5 20l-6-6zm-5.5 0a4 4 0 1 1 0-8 4 4 0 0 1 0 8z"/></svg>
              )}
            </span>
            <input
              type="text"
              id="subscription-input"
              aria-label={t('sources.inputLabel', '订阅地址或公众号名称')}
              placeholder={t('sources.inputPlaceholder', '粘贴链接，或搜索公众号名称 / 微信号')}
              value={input}
              autoFocus
              onChange={(event) => handleChange(event.target.value)}
              onKeyDown={handleKeyDown}
            />
            {target === 'link' ? (
              <button
                className="btn subscribe-go"
                id="source-resolve"
                type="button"
                disabled={busy || !input.trim()}
                onClick={() => void resolve()}
              >
                {busy ? t('sources.resolving', '解析中…') : t('sources.resolve', '解析')}
              </button>
            ) : null}
          </div>

          {error ? <p className="settings-error" role="alert">{error}</p> : null}

          {!input.trim() && caixinLoadError ? (
            <p className="settings-error" role="alert">
              {caixinLoadError}{' '}
              <button
                className="btn ghost"
                type="button"
                disabled={caixinLoading}
                onClick={() => {
                  setCaixinLoading(true);
                  setCaixinLoadError('');
                  setCaixinOpen(false);
                  setCaixinRetry((value) => value + 1);
                }}
              >
                {t('common.retry', '重试')}
              </button>
            </p>
          ) : null}

          {target === 'link' && !preview && !busy ? (
            <p className="muted subscribe-hint">
              {t('sources.addHint', '支持 B站视频 / UP、YouTube 频道、播客与公众号文章链接。')}
            </p>
          ) : null}

          {!input.trim() && caixinChannels?.length ? (
            <div className="source-preview">
              <div className="source-preview-main">
                <div>
                  <div>{t('sources.caixinTitle', '财新')}</div>
                  <div className="muted">{t('sources.caixinHint', '选择感兴趣的板块，不需要粘贴地址。')}</div>
                </div>
              </div>
              <button
                className="btn ghost"
                type="button"
                aria-expanded={caixinOpen}
                aria-controls="caixin-channel-list"
                onClick={() => setCaixinOpen((open) => !open)}
              >
                {caixinOpen ? t('common.close', '关闭') : t('sources.chooseChannels', '选择板块')}
              </button>
            </div>
          ) : null}

          {caixinOpen ? (
            <div
              className="search-results"
              id="caixin-channel-list"
              aria-label={t('sources.caixinChannels', '财新板块')}
            >
              {caixinChannels?.map((channel) => (
                <div key={channel.biz} className="search-item">
                  <div className="search-meta">
                    <div className="account-name">{channel.name}</div>
                    <div className="account-sub">
                      {channel.subscribed
                        ? t('sources.alreadySubscribed', '已经订阅过了。')
                        : channel.collecting
                          ? t('sources.collecting', '正在采集')
                          : channel.has_articles
                            ? t('sources.historyOnly', '暂停更新，可订阅历史内容')
                            : t('sources.notCollecting', '管理员尚未启用')}
                    </div>
                  </div>
                  <div className="search-actions">
                    {channel.subscribed ? (
                      <span className="muted">{t('sources.subscribedShort', '已订阅')}</span>
                    ) : channel.can_subscribe ? (
                      <button
                        className="btn ghost"
                        type="button"
                        disabled={Boolean(caixinBusy)}
                        onClick={() => void subscribeCaixin(channel)}
                      >
                        {caixinBusy === channel.biz ? t('common.loading', '正在加载…') : t('sources.subscribe', '订阅')}
                      </button>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {preview ? (
            <div className="source-preview" id="source-preview">
              <div className="source-preview-main">
                {preview.avatar_url ? <img className="account-avatar" src={preview.avatar_url} alt="" /> : null}
                <div>
                  <div>{preview.nickname}</div>
                  <div className="muted">{getSourceLabel(preview.source, t)} · {preview.biz}</div>
                </div>
              </div>
              {preview.subscribed ? (
                <p className="muted">{t('sources.alreadySubscribed', '已经订阅过了。')}</p>
              ) : (
                <button className="btn" id="source-subscribe" type="button" disabled={busy} onClick={() => void subscribeSource()}>
                  {t('sources.subscribe', '订阅')}
                </button>
              )}
            </div>
          ) : null}

          <LoadingIndicator isLoading={searching} id="subscription-search-loading" />

          {hits.length ? (
            <div className="search-results" id="subscription-search-results">
              {hits.map((hit) => (
                <div key={hit.biz} className="search-item">
                  {/* 头像加载失败时只能隐藏、不能摘除：它是 grid 的第一列，
                      元素消失会把 meta 挤进 42px 的列里，整行跟着错位。 */}
                  {hit.avatar_url ? (
                    <img
                      className="account-avatar"
                      src={hit.avatar_url}
                      alt=""
                      onError={(event) => { (event.target as HTMLImageElement).style.visibility = 'hidden'; }}
                    />
                  ) : (
                    <div className="account-avatar"></div>
                  )}
                  <div className="search-meta">
                    <div className="account-name">{hit.nickname || ''}</div>
                    <div className="account-sub">{hit.alias || hit.biz || ''}</div>
                  </div>
                  <div className="search-actions">
                    {hit.is_added ? (
                      <span className="meta-note">{t('accounts.searchAdded', '已添加')}</span>
                    ) : (
                      <button
                        className="btn ghost search-add"
                        type="button"
                        disabled={adding === hit.biz}
                        onClick={() => void addAccount(hit)}
                      >
                        {t('accounts.searchAdd', '添加')}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {target === 'name' && input.trim().length >= 2 && !hits.length && !searching ? (
            <p className="muted subscribe-hint">{t('accounts.searchEmpty', '没有找到匹配的公众号。')}</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}
