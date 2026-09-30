import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { ConfirmModal } from '../../components/ConfirmModal';

interface Provider {
  id: number;
  name: string;
  base_url: string;
  api_key_masked: string;
  model: string;
  is_default: boolean;
  enabled: boolean;
}

interface ProbeResult {
  ok: boolean;
  latency_ms: number | null;
  error?: string;
  models?: string[];
  model_available?: boolean | null;
}

const EMPTY_DRAFT = {
  name: '',
  base_url: '',
  api_key: '',
  model: '',
  is_default: false,
  enabled: true,
};

export function AdminLlmPage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState({ ...EMPTY_DRAFT });
  const [editing, setEditing] = useState<Provider | null>(null);
  const [probes, setProbes] = useState<Record<number, ProbeResult>>({});
  const [probing, setProbing] = useState<number | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Provider | null>(null);

  const load = useCallback(async () => {
    try {
      const payload = await apiGet('/api/llm/provider');
      setProviders((payload.items as Provider[]) ?? []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async () => {
    if (!draft.name || !draft.base_url || !draft.model || (!editing && !draft.api_key)) {
      showToast(t('admin.llm.missing', 'name、base_url、api_key、model 均为必填'));
      return;
    }
    try {
      if (editing) {
        // A blank key means "unchanged": the server keeps the stored value.
        await apiSend(`/api/llm/provider/${editing.id}`, 'PATCH', {
          name: draft.name,
          base_url: draft.base_url,
          model: draft.model,
          is_default: draft.is_default,
          enabled: draft.enabled,
          ...(draft.api_key ? { api_key: draft.api_key } : {}),
        });
        showToast(t('admin.llm.updated', 'provider 已更新'));
      } else {
        await apiSend('/api/llm/provider', 'POST', draft);
        showToast(t('admin.llm.created', 'provider 已创建'));
      }
      setDraft({ ...EMPTY_DRAFT });
      setEditing(null);
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  const startEdit = (provider: Provider) => {
    setEditing(provider);
    setDraft({
      name: provider.name,
      base_url: provider.base_url,
      api_key: '',
      model: provider.model,
      is_default: provider.is_default,
      enabled: provider.enabled,
    });
  };

  const probe = async (provider: Provider) => {
    setProbing(provider.id);
    try {
      const result = (await apiSend(`/api/llm/provider/${provider.id}/test`, 'POST', {})) as unknown as ProbeResult;
      setProbes((current) => ({ ...current, [provider.id]: result }));
    } catch (error) {
      setProbes((current) => ({
        ...current,
        [provider.id]: { ok: false, latency_ms: null, error: error instanceof Error ? error.message : String(error) },
      }));
    } finally {
      setProbing(null);
    }
  };

  const removeProvider = async (provider: Provider) => {
    await apiSend(`/api/llm/provider/${provider.id}`, 'DELETE', {});
    await load();
  };

  return (
    <div className="admin-page">
      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.llm.title', 'LLM Provider')}</h2>
          <span className="muted">
            {t('admin.llm.hint', '环境变量 HIPPO_LLM_* 仅用于首次初始化')}
          </span>
        </div>

        {loading ? (
          <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>
        ) : providers.length === 0 ? (
          <div className="muted admin-empty">{t('admin.llm.none', '还没有配置 provider')}</div>
        ) : (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>{t('admin.llm.name', '名称')}</th>
                  <th>{t('admin.llm.baseUrl', 'Base URL')}</th>
                  <th>{t('admin.llm.model', '模型')}</th>
                  <th>{t('admin.llm.key', 'API Key')}</th>
                  <th>{t('admin.llm.status', '状态')}</th>
                  <th>{t('admin.user.actions', '操作')}</th>
                </tr>
              </thead>
              <tbody>
                {providers.map((provider) => {
                  const result = probes[provider.id];
                  return (
                    <tr key={provider.id}>
                      <td>
                        {provider.name}
                        {provider.is_default ? (
                          <span className="admin-badge">{t('admin.llm.default', '默认')}</span>
                        ) : null}
                        {!provider.enabled ? (
                          <span className="admin-badge admin-badge-warn">
                            {t('admin.llm.disabled', '已停用')}
                          </span>
                        ) : null}
                      </td>
                      <td className="admin-mono">{provider.base_url}</td>
                      <td className="admin-mono">{provider.model}</td>
                      <td className="admin-mono">{provider.api_key_masked}</td>
                      <td>
                        {result ? (
                          <span className={result.ok ? 'admin-ok' : 'admin-fail'}>
                            {result.ok
                              ? t('admin.llm.probeOk', '可用 {ms}ms').replace(
                                  '{ms}',
                                  String(result.latency_ms ?? '?'),
                                )
                              : result.error || t('admin.llm.probeFail', '不可用')}
                          </span>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td className="admin-actions">
                        <button
                          className="btn ghost"
                          type="button"
                          disabled={probing === provider.id}
                          onClick={() => void probe(provider)}
                        >
                          {probing === provider.id
                            ? t('admin.llm.testing', '测试中…')
                            : t('admin.llm.test', '测试')}
                        </button>
                        <button className="btn ghost" type="button" onClick={() => startEdit(provider)}>
                          {t('admin.llm.edit', '编辑')}
                        </button>
                        <button className="btn ghost" type="button" onClick={() => setPendingDelete(provider)}>
                          {t('common.delete', '删除')}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">
            {editing
              ? t('admin.llm.editTitle', '编辑 {name}').replace('{name}', editing.name)
              : t('admin.llm.createTitle', '新增 Provider')}
          </h2>
        </div>
        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.llm.name', '名称')}</span>
            <input
              className="input"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.llm.baseUrl', 'Base URL')}</span>
            <input
              className="input"
              placeholder="http://localhost:8000/v1"
              value={draft.base_url}
              onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.llm.model', '模型')}</span>
            <input
              className="input"
              value={draft.model}
              onChange={(event) => setDraft({ ...draft, model: event.target.value })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.llm.key', 'API Key')}</span>
            <input
              className="input"
              type="password"
              autoComplete="off"
              placeholder={
                editing
                  ? t('admin.llm.keyKeep', '留空表示不修改')
                  : t('admin.llm.keyNew', '必填')
              }
              value={draft.api_key}
              onChange={(event) => setDraft({ ...draft, api_key: event.target.value })}
            />
          </label>
          <label className="auth-check">
            <input
              type="checkbox"
              checked={draft.is_default}
              onChange={(event) => setDraft({ ...draft, is_default: event.target.checked })}
            />
            <span>{t('admin.llm.setDefault', '设为默认')}</span>
          </label>
          <label className="auth-check">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
            />
            <span>{t('admin.llm.enabled', '启用')}</span>
          </label>
        </div>
        <div className="panel-footer">
          <button className="btn" type="button" onClick={() => void submit()}>
            {editing ? t('common.save', '保存') : t('admin.create.submit', '创建')}
          </button>
          {editing ? (
            <button
              className="btn ghost"
              type="button"
              onClick={() => {
                setEditing(null);
                setDraft({ ...EMPTY_DRAFT });
              }}
            >
              {t('common.cancel', '取消')}
            </button>
          ) : null}
        </div>
      </section>

      <ConfirmModal
        isOpen={pendingDelete !== null}
        title={t('admin.llm.deleteTitle', '删除 Provider')}
        message={t('admin.llm.deleteConfirm', '删除 {name}？引用它的会话会失去 provider。').replace(
          '{name}',
          pendingDelete?.name ?? '',
        )}
        confirmLabel={t('common.delete', '删除')}
        onConfirm={() => (pendingDelete ? removeProvider(pendingDelete) : Promise.resolve())}
        onClose={() => setPendingDelete(null)}
      />
    </div>
  );
}
