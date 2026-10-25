import { createElement, Fragment, type ReactNode } from 'react';

const parseWechatUrl = (urlStr: string) => {
  try {
    let fullUrl = urlStr.trim();
    if (fullUrl.startsWith('//')) fullUrl = `https:${fullUrl}`;
    if (!/^https?:\/\//i.test(fullUrl)) return null;
    const url = new URL(fullUrl);
    if (url.hostname !== 'mp.weixin.qq.com' || url.username || url.password || url.port) return null;
    const biz = url.searchParams.get('__biz');
    const mid = url.searchParams.get('mid') || url.searchParams.get('appmsgid');
    const idx = url.searchParams.get('idx') || url.searchParams.get('itemidx');
    const sn = url.searchParams.get('sn');
    if (/^\/s\/[A-Za-z0-9_-]{1,128}$/.test(url.pathname)) return { biz, mid, idx };
    if (['/s', '/mp/appmsg/show'].includes(url.pathname) && biz && (sn || (mid && idx))) {
      return { biz, mid, idx };
    }
  } catch {
    return null;
  }
  return null;
};

const normalizeSafeUrl = (urlStr: string) => {
  const rawUrl = urlStr.trim().replace(/&amp;/gi, '&');
  const fullUrl = rawUrl.startsWith('//') ? `https:${rawUrl}` : rawUrl;
  if (!/^https?:\/\//i.test(fullUrl)) return null;
  try {
    const url = new URL(fullUrl);
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

export type InlineLinkRenderer = (href: string, label: ReactNode[], key: string) => ReactNode;

const renderDefaultLink: InlineLinkRenderer = (href, label, key) => {
  const meta = parseWechatUrl(href);
  return createElement('a', {
    key,
    href,
    target: '_blank',
    rel: 'noopener noreferrer',
    className: meta ? 'js-article-link' : undefined,
    'data-hippo-biz': meta?.biz,
    'data-hippo-mid': meta?.mid,
    'data-hippo-idx': meta?.idx,
  }, label);
};

const renderLineNodes = (
  line: string,
  keyPrefix: string,
  renderLink: InlineLinkRenderer,
  allowLinks = true,
): ReactNode[] => {
  const normalizedLine = line.replace(/\\\|/g, '|');
  // shortcut: this is an inline Markdown subset; use a parser if full CommonMark is needed.
  const pattern = /(?<fence>`+)(?<code>.*?)\k<fence>|\[(?<label>(?:[^[\]]|\[[^\]]*\])+)\]\((?<href>(?:[^\s()]|\([^()]*\))+)\)|<(?<auto>https?:\/\/[^\s<>]+)>|(?<bare>https?:\/\/[^\s<>`，。！？；：、”’》】）]+)|(?<stars>\*+)/gi;
  const frames: { marker: string; nodes: ReactNode[]; key: string }[] = [
    { marker: '', nodes: [], key: keyPrefix },
  ];
  let cursor = 0;
  let matchIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(normalizedLine))) {
    const key = `${keyPrefix}-${matchIndex++}`;
    let nodes = frames[frames.length - 1].nodes;
    nodes.push(...withCjkSpacing(normalizedLine.slice(cursor, match.index), `${key}-text`));
    const groups = match.groups!;

    if (groups.code !== undefined) {
      nodes.push(createElement('code', { key }, groups.code));
    } else if (groups.stars) {
      let remaining = groups.stars.length;
      while (remaining > 0) {
        const frame = frames[frames.length - 1];
        const marker = frame.marker && remaining >= frame.marker.length
          ? frame.marker
          : remaining >= 2 ? '**' : '*';
        remaining -= marker.length;
        if (frame.marker === marker) {
          frames.pop();
          frames[frames.length - 1].nodes.push(createElement(
            marker === '**' ? 'strong' : 'em', { key: frame.key }, frame.nodes,
          ));
        } else {
          frames.push({ marker, nodes: [], key: `${key}-${remaining}` });
        }
      }
    } else {
      let destination = groups.href ?? groups.auto ?? groups.bare;
      if (groups.bare) {
        destination = destination.replace(/[.,!?;:，。！？；：、”’》】）\]]+$/u, '');
        const closingStars = Math.min(
          destination.match(/\*+$/)?.[0].length ?? 0,
          frames.reduce((count, frame) => count + frame.marker.length, 0),
        );
        if (closingStars) destination = destination.slice(0, -closingStars);
        // Keep balanced URL parentheses, but leave prose/Markdown wrappers outside.
        while (destination.endsWith(')') &&
          destination.split(')').length > destination.split('(').length) {
          destination = destination.slice(0, -1);
        }
        pattern.lastIndex = match.index + destination.length;
      }
      const href = normalizeSafeUrl(destination);
      nodes = frames[frames.length - 1].nodes;
      if (href && allowLinks) {
        const label = groups.label !== undefined
          ? renderLineNodes(groups.label, `${key}-label`, renderLink, false)
          : withCjkSpacing(destination, `${key}-label`);
        nodes.push(renderLink(href, label, key));
      } else {
        nodes.push(...withCjkSpacing(normalizedLine.slice(match.index, pattern.lastIndex), key));
      }
    }
    cursor = pattern.lastIndex;
  }

  frames[frames.length - 1].nodes.push(...withCjkSpacing(normalizedLine.slice(cursor), `${keyPrefix}-tail`));
  while (frames.length > 1) {
    const frame = frames.pop()!;
    frames[frames.length - 1].nodes.push(frame.marker, ...frame.nodes);
  }
  return frames[0].nodes;
};

export const renderInlineNodes = (
  text: string,
  renderLink: InlineLinkRenderer = renderDefaultLink,
): ReactNode[] => {
  const lines = text.split('\n');
  const nodes: ReactNode[] = [];

  lines.forEach((line, lineIndex) => {
    if (lineIndex > 0) {
      nodes.push(createElement('br', { key: `br-${lineIndex}` }));
    }
    const lineNodes = renderLineNodes(line, `line-${lineIndex}`, renderLink);
    if (!lineNodes.length) {
      nodes.push(createElement(Fragment, { key: `empty-${lineIndex}` }));
      return;
    }
    nodes.push(...lineNodes);
  });

  return nodes;
};

export { normalizeSafeUrl, parseWechatUrl };
