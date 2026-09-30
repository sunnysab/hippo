import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { useAuth } from '../../hooks/useAuth';

interface ReportArticle {
  id: number;
  title: string;
  digest: string | null;
  link: string;
  nickname: string | null;
  biz: string;
  display_time?: string;
}

interface ReportGroup {
  id: number | null;
  name: string;
  articles: ReportArticle[];
}

interface ReportPayload {
  date: string;
  timezone: string;
  total: number;
  groups: ReportGroup[];
}

interface ReportSetting {
  enabled: boolean;
  send_hour: number;
  recipients: string[];
  group_ids: number[];
  include_read: boolean;
}

interface GroupOption {
  id: number;
  name: string;
}

const todayISO = () => new Date().toISOString().slice(0, 10);

export function ReportPage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const { user } = useAuth();
  const [date, setDate] = useState(todayISO);
  const [payload, setPayload] = useState<ReportPayload | null>(null);
  const [setting, setSetting] = useState<ReportSetting | null>(null);
  const [groups, setGroups] = useState<GroupOption[]>([]);
  const [selectedGroups, setSelectedGroups] = useState<number[]>([]);
  const [loading, setLoading] = useState(true);

  const query = useMemo(
    () => (selectedGroups.length ? `?group_ids=${selectedGroups.join(',')}` : ''),
    [selectedGroups],
  );

  // Fetch inside the effect: the state writes then land after an await, which
  // keeps the effect from cascading renders.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [reportPayload, settingPayload] = await Promise.all([
          apiGet(`/api/report/${date}${query}`),
          apiGet('/api/report/setting'),
        ]);
        if (cancelled) return;
        setPayload(reportPayload as unknown as ReportPayload);
        setSetting(settingPayload as unknown as ReportSetting);
      } catch (error) {
        if (!cancelled) showToast(error instanceof Error ? error.message : String(error));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [date, query, showToast]);

  useEffect(() => {
    void (async () => {
      try {
        const response = await apiGet('/api/group');
        setGroups((response.groups as GroupOption[]) ?? []);
      } catch {
        /* the filter is optional */
      }
    })();
  }, []);

  const saveSetting = async (updates: Partial<ReportSetting>) => {
    try {
      const result = await apiSend('/api/report/setting', 'PATCH', updates);
      setSetting(result as unknown as ReportSetting);
      showToast(t('report.saved', '日报设置已保存'));
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  const sendNow = async () => {
    try {
      const result = await apiSend(`/api/report/${date}/send`, 'POST', {});
      showToast(
        result.sent
          ? t('report.sent', '已发送')
          : t('report.notSent', '未发送：{reason}').replace('{reason}', String(result.reason ?? '')),
      );
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div className="report-layout">
      <section className="panel report-toolbar">
        <label className="auth-field">
          <span>{t('report.date', '日期')}</span>
          <input
            className="input"
            type="date"
            value={date}
            onChange={(event) => setDate(event.target.value)}
          />
        </label>
        <div className="report-groups">
          <span className="muted">{t('report.groups', '分组')}</span>
          {groups.map((group) => (
            <label key={group.id} className="auth-check">
              <input
                type="checkbox"
                checked={selectedGroups.includes(group.id)}
                onChange={(event) =>
                  setSelectedGroups((current) =>
                    event.target.checked
                      ? [...current, group.id]
                      : current.filter((id) => id !== group.id),
                  )
                }
              />
              <span>{group.name}</span>
            </label>
          ))}
        </div>
        <div className="panel-footer">
          <span className="muted">
            {t('report.summary', '共 {n} 篇 · 时区 {tz}')
              .replace('{n}', String(payload?.total ?? 0))
              .replace('{tz}', payload?.timezone ?? user?.timezone ?? '')}
          </span>
          <button className="btn ghost" type="button" onClick={() => void sendNow()}>
            {t('report.sendNow', '立即发送')}
          </button>
        </div>
      </section>

      <section className="panel report-preview">
        {loading ? (
          <p className="muted admin-empty">{t('admin.loading', '加载中…')}</p>
        ) : !payload || payload.total === 0 ? (
          <p className="muted admin-empty">{t('report.none', '这一天没有新文章')}</p>
        ) : (
          payload.groups.map((group) => (
            <div key={group.id ?? 'ungrouped'} className="report-group">
              <h3 className="report-group-title">{group.name}</h3>
              <ul className="report-list">
                {group.articles.map((article) => (
                  <li key={article.id} className="report-item">
                    <a
                      className="report-link"
                      href={`#/articles?article=${article.id}`}
                      title={t('report.openInApp', '在 Hippo 中打开')}
                    >
                      {article.title}
                    </a>
                    <div className="muted report-meta">
                      {article.nickname || article.biz}
                      {article.display_time ? ` · ${article.display_time}` : ''}
                      {' · '}
                      <a href={article.link} target="_blank" rel="noreferrer noopener">
                        {t('report.original', '原文')}
                      </a>
                    </div>
                    {article.digest ? <p className="report-digest">{article.digest}</p> : null}
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('report.settingTitle', '日报设置')}</h2>
        </div>
        {setting ? (
          <div className="report-settings">
            <label className="auth-check">
              <input
                type="checkbox"
                checked={setting.enabled}
                onChange={(event) => void saveSetting({ enabled: event.target.checked })}
              />
              <span>{t('report.enabled', '启用日报邮件')}</span>
            </label>
            <label className="auth-field">
              <span>{t('report.sendHour', '发送时间（{tz} 的整点）').replace('{tz}', payload?.timezone ?? user?.timezone ?? '')}</span>
              <select
                className="input"
                value={setting.send_hour}
                onChange={(event) => void saveSetting({ send_hour: Number(event.target.value) })}
              >
                {Array.from({ length: 24 }, (_, hour) => (
                  <option key={hour} value={hour}>
                    {String(hour).padStart(2, '0')}:00
                  </option>
                ))}
              </select>
            </label>
            <label className="auth-field">
              <span>{t('report.recipients', '收件人（逗号分隔）')}</span>
              <input
                className="input"
                defaultValue={setting.recipients.join(', ')}
                onBlur={(event) =>
                  void saveSetting({
                    recipients: event.target.value
                      .split(',')
                      .map((item) => item.trim())
                      .filter(Boolean),
                  })
                }
              />
            </label>
            <label className="auth-check">
              <input
                type="checkbox"
                checked={setting.include_read}
                onChange={(event) => void saveSetting({ include_read: event.target.checked })}
              />
              <span>{t('report.includeRead', '包含已读文章')}</span>
            </label>
            <label className="auth-field">
              <span>{t('report.scope', '默认包含的分组（留空为全部）')}</span>
              <select
                className="input"
                multiple
                value={setting.group_ids.map(String)}
                onChange={(event) =>
                  void saveSetting({
                    group_ids: Array.from(event.target.selectedOptions).map((option) =>
                      Number(option.value),
                    ),
                  })
                }
              >
                {groups.map((group) => (
                  <option key={group.id} value={group.id}>
                    {group.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        ) : null}
      </section>
    </div>
  );
}
