import { useEffect, useState } from 'react';
import { useSettingsState } from '../../store/settings';
import { useI18n } from '../../i18n';
import { formatRelativeTime } from '../../utils/format';

export function SyncHealthPanel() {
  const { state } = useSettingsState();
  const { t } = useI18n();
  const status = state.syncStatus;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 10000);
    return () => clearInterval(timer);
  }, []);
  const heartbeat = status?.worker_heartbeat_at;
  const alive = heartbeat && now - new Date(heartbeat).getTime() < 120000;
  const pushStatus = status?.push?.status;
  const labels: Record<string, string> = { listening: '推送监听已启动', reconnecting: '推送监听正在重连', stopped: '推送监听已停止' };
  return <section className="panel">
    <h2>{t('sync.healthTitle', '后台与推送')}</h2>
    <p>{!status ? t('common.loading', '加载中…') : !heartbeat ? t('sync.workerUnknown', '尚无 worker 心跳')
      : alive ? t('sync.workerOnline', 'worker 正常运行') : t('sync.workerStale', 'worker 心跳已过期，请检查后台服务')}</p>
    <p>{t(`sync.push.${alive ? pushStatus || 'unknown' : 'unknown'}`, alive && pushStatus ? labels[pushStatus] || pushStatus : '推送监听状态未知')}</p>
    {status?.push?.last_received_at && <p>{t('sync.lastPush', '最近收到推送：{time}').replace('{time}', formatRelativeTime(status.push.last_received_at, t))}</p>}
    {status?.schedule && <p>{status.schedule.enabled
      ? status.schedule.next_eligible_at
        ? t('sync.nextEligible', '下次近期检查最早可执行时间：{time}（队列空闲后执行）').replace('{time}', new Date(status.schedule.next_eligible_at).toLocaleString())
        : t('sync.scheduleWaiting', '定时近期检查已开启，等待调度')
      : t('sync.scheduleOff', '定时近期检查已关闭；推送与已入队工作继续执行。')}</p>}
  </section>;
}
