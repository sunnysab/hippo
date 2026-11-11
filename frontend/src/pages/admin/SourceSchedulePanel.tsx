import { useEffect, useState, type FormEvent } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';

export function SourceSchedulePanel({ source }: { source: string }) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const caixin = source === 'caixin';
  const [intervalValue, setIntervalValue] = useState('1');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [loadAttempt, setLoadAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void apiGet('/api/settings', { signal: controller.signal }).then((payload) => {
      if (controller.signal.aborted) return;
      const intervals = payload.source_intervals_days as Record<string, number> | undefined;
      setIntervalValue(String(caixin ? payload.caixin_interval_hours ?? 6 : intervals?.[source] ?? 1));
    }).catch((err: unknown) => {
      if (!controller.signal.aborted && !isAuthError(err)) setError((err as Error).message);
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [caixin, source, loadAttempt]);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const interval = Number(intervalValue);
    if (!Number.isInteger(interval) || interval < 1) return;
    setSaving(true);
    try {
      const payload = await apiSend('/api/settings', 'PATCH', caixin
        ? { caixin_interval_hours: interval }
        : { source_intervals_days: { [source]: interval } });
      setIntervalValue(String(caixin
        ? payload.caixin_interval_hours
        : (payload.source_intervals_days as Record<string, number>)[source]));
      showToast(t('sync.saved', '同步设置已保存'));
    } catch (err) {
      if (!isAuthError(err)) showToast((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="panel" onSubmit={(event) => void save(event)}>
      <div className="panel-header">
        <h2>{t('sync.sourceSchedule', '定时更新')}</h2>
        <button className="btn" type="submit" disabled={loading || saving || Boolean(error)}>
          {t('sync.save', '保存设置')}
        </button>
      </div>
      {error ? (
        <p role="alert">{error} <button className="btn ghost" type="button" onClick={() => {
          setLoading(true);
          setError('');
          setLoadAttempt((attempt) => attempt + 1);
        }}>{t('common.retry', '重试')}</button></p>
      ) : loading ? (
        <p className="muted">{t('admin.loading', '加载中…')}</p>
      ) : (
        <label>
          <span>{caixin
            ? t('sync.caixinIntervalHours', '财新检查间隔（小时）')
            : t('sync.sourceIntervalDays', '默认更新间隔（天）')}</span>
          <input
            type="number"
            min="1"
            step="1"
            required
            value={intervalValue}
            onChange={(event) => setIntervalValue(event.target.value)}
          />
        </label>
      )}
      {caixin && !loading && !error ? (
        <p className="muted">
          {t('sync.caixinHistoryHint', '先检查各板块的新文章，每轮再轮换补抓一页旧文章。')}
        </p>
      ) : null}
    </form>
  );
}
