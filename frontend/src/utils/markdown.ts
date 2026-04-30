import { escapeHtml } from './format';

const parseWechatUrl = (urlStr: string) => {
  try {
    let fullUrl = urlStr.trim();
    if (fullUrl.startsWith('//')) fullUrl = `https:${fullUrl}`;
    if (!/^https?:\/\//i.test(fullUrl)) return null;
    const url = new URL(fullUrl);
    if (url.hostname !== 'mp.weixin.qq.com') return null;
    const biz = url.searchParams.get('__biz');
    const mid = url.searchParams.get('mid');
    const idx = url.searchParams.get('idx');
    if (biz && mid && idx) return { biz, mid, idx };
  } catch {
    return null;
  }
  return null;
};

export const renderInline = (text: string): string => {
  const escaped = escapeHtml(text);

  return escaped
    .replace(/\\\|/g, '|')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label: string, url: string) => {
      const rawUrl = url.replace(/&amp;/g, '&').trim();
      const meta = parseWechatUrl(rawUrl);
      if (meta) {
        return `<a href="${url}" target="_blank" rel="noopener noreferrer" data-hippo-biz="${meta.biz}" data-hippo-mid="${meta.mid}" data-hippo-idx="${meta.idx}" class="js-article-link">${label}</a>`;
      }
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    })
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br>');
};

export { parseWechatUrl };
