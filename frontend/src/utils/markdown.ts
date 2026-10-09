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

// text-autospace is not implemented everywhere yet, so plain text is split on
// CJK/Latin boundaries as well. Engines that do implement it keep native
// spacing rules and get no injected spacer.
const AUTOSPACE_SUPPORTED =
  typeof CSS !== 'undefined' && CSS.supports?.('text-autospace', 'normal') === true;

const CJK_RANGE =
  '\u1100-\u11ff\u2e80-\u303f\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef';
const CJK_BOUNDARY = new RegExp(`([${CJK_RANGE}])([A-Za-z0-9])|([A-Za-z0-9])([${CJK_RANGE}])`, 'g');

/** Split CJK<->Latin/digit boundaries so `.cjk-spacer` can supply the gap. */
const withCjkSpacing = (text: string, key: string): ReactNode[] => {
  if (AUTOSPACE_SUPPORTED || !text) return [text];

  const nodes: ReactNode[] = [];
  let cursor = 0;
  let index = 0;

  for (const match of text.matchAll(CJK_BOUNDARY)) {
    const start = match.index ?? 0;
    nodes.push(
      text.slice(cursor, start + 1),
      createElement('span', {
        key: `${key}-space-${index}`,
        className: 'cjk-spacer',
        'aria-hidden': true,
      }),
    );
    cursor = start + 1;
    index += 1;
  }

  if (!nodes.length) return [text];
  nodes.push(text.slice(cursor));
  return nodes;
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
      nodes.push(...withCjkSpacing(normalizedLine.slice(cursor, start), `text-${lineIndex}-${matchIndex}`));
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
        }, withCjkSpacing(label, `link-${lineIndex}-${matchIndex}`)));
      }
    } else if (match[3]) {
      nodes.push(createElement('strong', { key: `strong-${lineIndex}-${matchIndex}` }, withCjkSpacing(match[3], `strong-${lineIndex}-${matchIndex}`)));
    } else if (match[4]) {
      nodes.push(createElement('em', { key: `em-${lineIndex}-${matchIndex}` }, withCjkSpacing(match[4], `em-${lineIndex}-${matchIndex}`)));
    } else if (match[5]) {
      nodes.push(createElement('code', { key: `code-${lineIndex}-${matchIndex}` }, match[5]));
    } else {
      nodes.push(...withCjkSpacing(match[0], `raw-${lineIndex}-${matchIndex}`));
    }

    cursor = start + match[0].length;
    matchIndex += 1;
  }

  if (cursor < normalizedLine.length) {
    nodes.push(...withCjkSpacing(normalizedLine.slice(cursor), `tail-${lineIndex}`));
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
