import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { formatDateTime } from '../../utils/format';

interface AuditEntry {
  id: number;
  user_id: number | null;
  username: string | null;
  action: string;
  target: string | null;
  detail: Record<string, unknown> | null;
  ip: string | null;
  created_at: string | null;
}

interface AuditPage {
  items: AuditEntry[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
}

interface TailResult {
  available: boolean;
  reason?: string;
  path?: string;
  lines: string[];
}

const RANGES = [
  { key: '60', labelKey: 'admin.log.lastHour', label: '最近 1 小时' },
  { key: '1440', labelKey: 'admin.log.lastDay', label: '最近 24 小时' },
  { key: '0', labelKey: 'admin.log.all', label: '全部' },
];

const formatTime = (value: string | null) => formatDateTime(value) || '—';

export function AdminLogPage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [page, setPage] = useState(1);
  const [action, setAction] = useState('');
  const [actions, setActions] = useState<string[]>([]);
  const [range, setRange] = useState('60');
  const [data, setData] = useState<AuditPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [tail, setTail] = useState<TailResult | null>(null);
  const [showTail, setShowTail] = useState(false);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ page: String(page), page_size: '50' });
    if (action) params.set('action', action);
    if (range !== '0') {
      const since = new Date(Date.now() - Number(range) * 60_000).toISOString();
      params.set('since', since);
    }
    try {
      const payload = await apiGet(`/api/admin/audit?${params.toString()}`);
      setData(payload as unknown as AuditPage);
    } finally {
      setLoading(false);
    }
  }, [action, page, range]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void (async () => {
      try {
        const payload = await apiGet('/api/admin/audit/action');
        setActions((payload.items as string[]) ?? []);
      } catch {
        /* the dropdown is a convenience; the table still works without it */
      }
    })();
  }, []);

  const openSignoz = async () => {
    try {
      const payload = await apiSend('/api/admin/log/link', 'POST', { minutes: Number(range) || 60 });
      if (!payload.available) {
        showToast(String(payload.reason ?? t('admin.log.noSignoz', '未配置 SignOz')));
        return;
      }
      window.open(String(payload.url), '_blank', 'noopener');
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  const loadTail = async () => {
    const payload = await apiGet('/api/admin/log/tail?lines=200');
    setTail(payload as unknown as TailResult);
    setShowTail(true);
  };

  return (
    <div className="admin-page">
      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.log.auditTitle', '审计日志')}</h2>
          <div className="admin-actions">
            <button className="btn ghost" type="button" onClick={() => void openSignoz()}>
              {t('admin.log.openSignoz', '在 SignOz 中查看运行日志')}
            </button>
            <button className="btn ghost" type="button" onClick={() => void loadTail()}>
              {t('admin.log.showTail', '兜底：文件日志')}
            </button>
          </div>
        </div>

        <div className="admin-filters">
          <label className="auth-field">
            <span>{t('admin.log.action', '操作')}</span>
            <select
              className="input"
              value={action}
              onChange={(event) => {
                setAction(event.target.value);
                setPage(1);
              }}
            >
              <option value="">{t('admin.log.allActions', '全部')}</option>
              {actions.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label className="auth-field">
            <span>{t('admin.log.range', '时间范围')}</span>
            <select
              className="input"
              value={range}
              onChange={(event) => {
                setRange(event.target.value);
                setPage(1);
              }}
            >
              {RANGES.map((item) => (
                <option key={item.key} value={item.key}>
                  {t(item.labelKey, item.label)}
                </option>
              ))}
            </select>
          </label>
        </div>

        {loading ? (
          <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>
        ) : !data || data.items.length === 0 ? (
          <div className="muted admin-empty">{t('admin.log.empty', '没有匹配的记录')}</div>
        ) : (
          <>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>{t('admin.log.time', '时间')}</th>
                    <th>{t('admin.log.actor', '操作者')}</th>
                    <th>{t('admin.log.action', '操作')}</th>
                    <th>{t('admin.log.target', '对象')}</th>
                    <th>{t('admin.log.detail', '详情')}</th>
                    <th>{t('admin.log.ip', 'IP')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((entry) => (
                    <tr key={entry.id}>
                      <td className="admin-nowrap">{formatTime(entry.created_at)}</td>
                      <td>{entry.username ?? (entry.user_id ?? '—')}</td>
                      <td className="admin-mono">{entry.action}</td>
                      <td>{entry.target ?? '—'}</td>
                      <td
                        className="admin-mono admin-detail"
                        title={entry.detail ? JSON.stringify(entry.detail) : undefined}
                      >
                        {entry.detail ? JSON.stringify(entry.detail) : '—'}
                      </td>
                      <td className="admin-mono">{entry.ip ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="panel-footer">
              <button
                className="btn ghost"
                type="button"
                disabled={page <= 1}
                onClick={() => setPage((value) => value - 1)}
              >
                {t('admin.log.prev', '上一页')}
              </button>
              <span className="muted">
                {t('admin.log.pageOf', '第 {page} / {pages} 页，共 {total} 条')
                  .replace('{page}', String(data.page))
                  .replace('{pages}', String(data.pages))
                  .replace('{total}', String(data.total))}
              </span>
              <button
                className="btn ghost"
                type="button"
                disabled={page >= data.pages}
                onClick={() => setPage((value) => value + 1)}
              >
                {t('admin.log.next', '下一页')}
              </button>
            </div>
          </>
        )}
      </section>

      {showTail ? (
        <section className="panel">
          <div className="panel-header">
            <h2 className="panel-title">{t('admin.log.tailTitle', '文件日志（兜底）')}</h2>
            <button className="btn ghost" type="button" onClick={() => setShowTail(false)}>
              {t('admin.log.hideTail', '收起')}
            </button>
          </div>
          {tail?.available ? (
            <>
              <p className="muted admin-note">{tail.path}</p>
              <pre className="admin-tail">{tail.lines.join('\n')}</pre>
            </>
          ) : (
            <div className="muted admin-empty">{tail?.reason ?? t('admin.log.tailUnavailable', '不可用')}</div>
          )}
        </section>
      ) : null}
    </div>
  );
}
