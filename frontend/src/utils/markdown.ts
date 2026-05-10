import { createElement, Fragment, type ReactNode } from 'react';

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

const normalizeSafeUrl = (urlStr: string) => {
  const rawUrl = urlStr.trim();
  if (rawUrl.startsWith('//')) return `https:${rawUrl}`;
  if (!/^https?:\/\//i.test(rawUrl)) return null;
  try {
    const url = new URL(rawUrl);
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    return url.toString();
  } catch {
    return null;
  }
};

const renderLineNodes = (line: string, lineIndex: number): ReactNode[] => {
  const normalizedLine = line.replace(/\\\|/g, '|');
  const nodes: ReactNode[] = [];
  const pattern = /\[([^\]]+)\]\(([^)]+)\)|\*\*(.+?)\*\*|\*(.+?)\*|`([^`]+)`/g;
  let cursor = 0;
  let matchIndex = 0;

  for (const match of normalizedLine.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > cursor) {
      nodes.push(normalizedLine.slice(cursor, start));
    }

    if (match[1] && match[2]) {
      const label = match[1];
      const href = normalizeSafeUrl(match[2]);
      if (!href) {
        nodes.push(match[0]);
      } else {
        const meta = parseWechatUrl(href);
        nodes.push(createElement('a', {
          key: `link-${lineIndex}-${matchIndex}`,
          href,
          target: '_blank',
          rel: 'noopener noreferrer',
          className: meta ? 'js-article-link' : undefined,
          'data-hippo-biz': meta?.biz,
          'data-hippo-mid': meta?.mid,
          'data-hippo-idx': meta?.idx,
        }, label));
      }
    } else if (match[3]) {
      nodes.push(createElement('strong', { key: `strong-${lineIndex}-${matchIndex}` }, match[3]));
    } else if (match[4]) {
      nodes.push(createElement('em', { key: `em-${lineIndex}-${matchIndex}` }, match[4]));
    } else if (match[5]) {
      nodes.push(createElement('code', { key: `code-${lineIndex}-${matchIndex}` }, match[5]));
    } else {
      nodes.push(match[0]);
    }

    cursor = start + match[0].length;
    matchIndex += 1;
  }

  if (cursor < normalizedLine.length) {
    nodes.push(normalizedLine.slice(cursor));
  }

  return nodes;
};

export const renderInlineNodes = (text: string): ReactNode[] => {
  const lines = text.split('\n');
  const nodes: ReactNode[] = [];

  lines.forEach((line, lineIndex) => {
    if (lineIndex > 0) {
      nodes.push(createElement('br', { key: `br-${lineIndex}` }));
    }
    const lineNodes = renderLineNodes(line, lineIndex);
    if (!lineNodes.length) {
      nodes.push(createElement(Fragment, { key: `empty-${lineIndex}` }));
      return;
    }
    nodes.push(...lineNodes);
  });

  return nodes;
};

export { parseWechatUrl };
