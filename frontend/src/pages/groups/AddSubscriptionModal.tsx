import { useState } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { emitRefresh } from '../../utils/events';
import { getSourceLabel } from '../../utils/source';

interface AddSubscriptionModalProps {
  isOpen: boolean;
  onClose: () => void;
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

/**
 * Subscribe by pasting one address — a Bilibili video or UP, a YouTube channel, a
 * podcast feed. The preview exists so the reader sees what the URL resolved to
 * (and whether they already have it) before anything is written.
 */
export function AddSubscriptionModal({ isOpen, onClose, onSubscribed }: AddSubscriptionModalProps) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [url, setUrl] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  if (!isOpen) return null;

  const close = () => {
    setUrl('');
    setPreview(null);
    setError('');
    onClose();
  };

  const resolve = async () => {
    const target = url.trim();
    if (!target) return;
    setBusy(true);
    setError('');
    setPreview(null);
    try {
      const payload = await apiGet(
        `/api/source/preview?url=${encodeURIComponent(target)}`,
      ) as unknown as Preview;
      setPreview(payload);
    } catch (err) {
      if (isAuthError(err)) return;
      setError((err as Error)?.message || t('sources.resolveFailed', '解析失败。'));
    } finally {
      setBusy(false);
    }
  };

  const subscribe = async () => {
    setBusy(true);
    try {
      await apiSend('/api/source/channel', 'POST', { url: url.trim() });
      showToast(t('sources.subscribed', '已订阅，稍后会自动同步。'));
      emitRefresh();
      onSubscribed?.();
      close();
    } catch (err) {
      if (isAuthError(err)) return;
      setError((err as Error)?.message || t('sources.subscribeFailed', '订阅失败。'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay is-open" onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
      <div className="modal search-modal">
        <div className="modal-header">
          <div className="modal-title">{t('sources.addTitle', '添加订阅')}</div>
          <button className="icon-btn" type="button" aria-label={t('common.close', '关闭')} onClick={close}>✕</button>
        </div>
        <div className="modal-body">
          <p className="muted">
            {t('sources.addHint', '粘贴 B站视频/UP、YouTube 频道或播客的订阅地址。')}
          </p>
          <div className="toolbar">
            <div className="input">
              <input
                type="url"
                id="source-url"
                aria-label={t('sources.urlLabel', '订阅地址')}
                placeholder={t('sources.urlPlaceholder', 'https://space.bilibili.com/…')}
                value={url}
                onChange={(event) => {
                  setUrl(event.target.value);
                  setPreview(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void resolve();
                }}
              />
            </div>
            <button className="btn" id="source-resolve" type="button" disabled={busy || !url.trim()} onClick={() => void resolve()}>
              {busy ? t('sources.resolving', '解析中…') : t('sources.resolve', '解析')}
            </button>
          </div>

          {error ? <p className="settings-error" role="alert">{error}</p> : null}

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
                <button className="btn" id="source-subscribe" type="button" disabled={busy} onClick={() => void subscribe()}>
                  {t('sources.subscribe', '订阅')}
                </button>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
