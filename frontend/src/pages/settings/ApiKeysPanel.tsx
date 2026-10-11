import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { copyToClipboard } from '../../utils/clipboard';

interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  scopes: string[];
  last_used_at: string | null;
  last_used_ip: string | null;
  expires_at: string | null;
  created_at: string | null;
}

const formatMoment = (value: string | null): string => {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
};

/**
 * API keys for reading this instance from something that cannot hold a cookie.
 *
 * The secret is shown once, in the panel that created it, and never stored in a
 * way that could bring it back: the list is built from a prefix.
 */
export function ApiKeysPanel() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<{ secret: string; name: string } | null>(null);

  const load = useCallback(async () => {
    const payload = await apiGet('/api/apikey') as { keys?: ApiKey[] };
    setKeys(payload.keys || []);
  }, []);

  // 初次读取写成 effect 里的 IIFE：状态更新都发生在 await 之后，不与渲染同步争抢。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await apiGet('/api/apikey') as { keys?: ApiKey[] };
        if (!cancelled) setKeys(payload.keys || []);
      } catch (error) {
        if (!isAuthError(error)) showToast((error as Error)?.message || t('apikeys.loadFailed', '读取失败。'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [showToast, t]);

  const create = async () => {
    setCreating(true);
    try {
      const payload = await apiSend('/api/apikey', 'POST', { name: name.trim() || 'API Key' }) as {
        secret?: string;
        name?: string;
      };
      if (payload.secret) setCreated({ secret: payload.secret, name: String(payload.name || '') });
      setName('');
      await load();
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error)?.message || t('apikeys.createFailed', '创建失败。'));
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (id: number) => {
    try {
      await apiSend(`/api/apikey/${id}`, 'DELETE', {});
      await load();
      showToast(t('apikeys.revoked', '已吊销。'));
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error)?.message || t('apikeys.revokeFailed', '吊销失败。'));
    }
  };

  if (loading) {
    return <div className="panel muted">{t('admin.loading', '加载中…')}</div>;
  }

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <h2>{t('apikeys.title', 'API 密钥')}</h2>
          <p className="muted">
            {t('apikeys.subtitle', '只读访问文章、转写稿与留档音频。管理接口与凭证接口不接受 API Key。')}
          </p>
        </div>
      </div>

      <div className="form-grid">
        <label className="auth-field">
          <span>{t('apikeys.name', '名称')}</span>
          <input
            className="input"
            id="apikey-name"
            placeholder={t('apikeys.namePlaceholder', '比如：笔记脚本')}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
      </div>
      <div className="toolbar">
        <button className="btn" id="apikey-create" type="button" disabled={creating} onClick={() => void create()}>
          {creating ? t('apikeys.creating', '创建中…') : t('apikeys.create', '创建密钥')}
        </button>
      </div>

      {created ? (
        <div className="api-key-secret" id="apikey-secret">
          <p className="muted">{t('apikeys.showOnce', '这是唯一一次显示明文，请立刻保存。')}</p>
          <code>{created.secret}</code>
          <div className="toolbar">
            <button
              className="btn ghost"
              type="button"
              onClick={async () => {
                await copyToClipboard(created.secret);
                showToast(t('apikeys.copied', '已复制。'));
              }}
            >
              {t('apikeys.copy', '复制')}
            </button>
            <button className="btn ghost" type="button" onClick={() => setCreated(null)}>
              {t('apikeys.dismiss', '我已保存')}
            </button>
          </div>
        </div>
      ) : null}

      {keys.length === 0 ? (
        <p className="muted admin-note">{t('apikeys.empty', '还没有密钥。')}</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>{t('apikeys.name', '名称')}</th>
              <th>{t('apikeys.prefix', '密钥')}</th>
              <th>{t('apikeys.scopes', '权限')}</th>
              <th>{t('apikeys.lastUsed', '最近使用')}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.map((key) => (
              <tr key={key.id}>
                <td>{key.name}</td>
                <td><code>{key.prefix}…</code></td>
                <td>{key.scopes.join(', ')}</td>
                <td className="muted">{formatMoment(key.last_used_at) || t('apikeys.neverUsed', '从未使用')}</td>
                <td>
                  <button className="btn ghost" type="button" onClick={() => void revoke(key.id)}>
                    {t('apikeys.revoke', '吊销')}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
