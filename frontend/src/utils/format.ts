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

export const formatRelativeTime = (isoString: string | null | undefined): string => {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  const diff = Date.now() - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  return `${days} 天前`;
};
