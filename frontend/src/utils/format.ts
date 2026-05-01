export const escapeHtml = (value: unknown): string =>
  String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

export const formatDate = (value: number | string | null | undefined): string => {
  if (!value) return '';
  let date: Date;
  if (typeof value === 'number') {
    date = new Date(value * 1000);
  } else {
    date = new Date(value);
  }
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('zh-CN');
};

export const formatDateTime = (value: number | string | null | undefined): string => {
  if (!value) return '';
  let date: Date;
  if (typeof value === 'number') {
    date = new Date(value * 1000);
  } else {
    date = new Date(value);
  }
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN');
};

type Translate = (key: string, fallback?: string) => string;

export const formatRelativeTime = (
  isoString: string | null | undefined,
  t?: Translate,
): string => {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  const diff = Date.now() - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return t ? t('time.justNow', 'Just now') : 'Just now';
  if (minutes < 60) {
    const template = t ? t('time.minutesAgo', '{n} minutes ago') : '{n} minutes ago';
    return template.replace('{n}', String(minutes));
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const template = t ? t('time.hoursAgo', '{n} hours ago') : '{n} hours ago';
    return template.replace('{n}', String(hours));
  }
  const days = Math.floor(hours / 24);
  const template = t ? t('time.daysAgo', '{n} days ago') : '{n} days ago';
  return template.replace('{n}', String(days));
};
