import { buildTextMap, CONTEXT_LENGTH, contextScore, locate, type Anchor, type TextMap } from './anchor';
import { inlineVisibleBoundary, inlineVisibleText } from '../../utils/markdown';

type Span = { original_start: number; original_end: number; start: number; end: number };
type Segment = { rawStart: number; rawEnd: number; liveStart: number; liveEnd: number };

export function visibleCorrectionSpans(original: string, display: string, spans: Span[]): Span[] | null {
  const visibleOriginal = inlineVisibleText(original);
  const visibleDisplay = inlineVisibleText(display);
  const converted: Span[] = [];
  const fallback = () => null;
  let rawEnd = 0;
  let liveEnd = 0;
  for (const span of spans) {
    const originalStart = inlineVisibleBoundary(original, span.original_start);
    const originalEnd = inlineVisibleBoundary(original, span.original_end);
    const start = inlineVisibleBoundary(display, span.start);
    const end = inlineVisibleBoundary(display, span.end);
    if (originalStart === null || originalEnd === null || start === null || end === null) {
      // A replacement can change Markdown parsing outside its own characters.
      // Refuse ambiguous anchors rather than save or display an unrelated range.
      return fallback();
    }
    if (originalStart < rawEnd || start < liveEnd || originalEnd < originalStart || end < start ||
      visibleOriginal.slice(rawEnd, originalStart) !== visibleDisplay.slice(liveEnd, start)) return fallback();
    converted.push({ original_start: originalStart, original_end: originalEnd, start, end });
    rawEnd = originalEnd;
    liveEnd = end;
  }
  return visibleOriginal.slice(rawEnd) === visibleDisplay.slice(liveEnd) ? converted : fallback();
}

/** Build offsets in UTF-16, the unit used by DOM ranges and the API. */
export function correctionMap(root: HTMLElement) {
  const clone = root.cloneNode(true) as HTMLElement;
  const segments: Segment[] = [];
  const ambiguous: Segment[] = [];
  let delta = 0;
  for (const node of Array.from(clone.querySelectorAll<HTMLElement>('[data-asr-original]'))) {
    const range = document.createRange();
    range.selectNodeContents(clone);
    range.setEndBefore(node);
    const liveStart = range.toString().length - delta;
    const original = node.dataset.asrOriginal || '';
    const liveLength = node.textContent?.length || 0;
    const spans = JSON.parse(node.dataset.asrSpans || '[]') as Span[] | null;
    if (spans === null) {
      const segment = { rawStart: liveStart + delta, rawEnd: liveStart + delta + original.length, liveStart, liveEnd: liveStart + liveLength };
      ambiguous.push(segment);
      segments.push(segment);
    }
    for (const span of spans ?? []) segments.push({
      rawStart: liveStart + delta + span.original_start,
      rawEnd: liveStart + delta + span.original_end,
      liveStart: liveStart + span.start,
      liveEnd: liveStart + span.end,
    });
    node.textContent = original;
    delta += original.length - liveLength;
  }
  const raw = buildTextMap(clone);
  const convert = (offset: number, toRaw: boolean, end: boolean) => {
    let shift = 0;
    for (const segment of segments) {
      const fromStart = toRaw ? segment.liveStart : segment.rawStart;
      const fromEnd = toRaw ? segment.liveEnd : segment.rawEnd;
      const toStart = toRaw ? segment.rawStart : segment.liveStart;
      const toEnd = toRaw ? segment.rawEnd : segment.liveEnd;
      if (offset <= fromStart) return offset + shift;
      if (offset < fromEnd) return end ? toEnd : toStart;
      shift = toEnd - fromEnd;
    }
    return offset + shift;
  };
  return {
    raw,
    canMap: (start: number, end: number, toRaw: boolean) => !ambiguous.some((segment) => {
      const from = toRaw ? segment.liveStart : segment.rawStart;
      const to = toRaw ? segment.liveEnd : segment.rawEnd;
      return from === to ? start <= from && end >= to : start < to && end > from;
    }),
    toRaw: (offset: number, end: boolean) => convert(offset, true, end),
    toLive: (offset: number, end: boolean) => convert(offset, false, end),
  };
}

export function rawSelection(root: HTMLElement, range: Range): Anchor | null {
  const before = document.createRange();
  before.selectNodeContents(root);
  try { before.setEnd(range.startContainer, range.startOffset); } catch { return null; }
  const start = before.toString().length;
  const end = start + range.toString().length;
  if (end <= start) return null;
  const map = correctionMap(root);
  if (!map.canMap(start, end, true)) return null;
  const rawStart = map.toRaw(start, false);
  const rawEnd = map.toRaw(end, true);
  const quote = map.raw.text.slice(rawStart, rawEnd);
  if (!quote.trim()) return null;
  return { quote, prefix: map.raw.text.slice(Math.max(0, rawStart - CONTEXT_LENGTH), rawStart), suffix: map.raw.text.slice(rawEnd, rawEnd + CONTEXT_LENGTH) };
}

export function locateRaw(map: TextMap, anchor: Anchor) {
  const found = locate(map, anchor);
  if (!found) return null;
  let index = map.text.indexOf(anchor.quote);
  while (index !== -1) {
    if (index !== found.start && contextScore(map.text, index, index + anchor.quote.length, anchor) === found.score) return null;
    index = map.text.indexOf(anchor.quote, index + 1);
  }
  return found;
}

export function eligibleSelection(range: Range): boolean {
  const element = (node: Node) => node instanceof Element ? node : node.parentElement;
  const start = element(range.startContainer)?.closest('[data-asr-original]');
  const end = element(range.endContainer)?.closest('[data-asr-original]');
  if (!start || start !== end || !range.toString().trim() || start.getAttribute('data-asr-spans') === 'null') return false;
  if (Array.from(start.querySelectorAll('a, code, .reader-anchor')).some((node) => range.intersectsNode(node))) return false;
  const before = document.createRange();
  before.selectNodeContents(start);
  try { before.setEnd(range.startContainer, range.startOffset); } catch { return false; }
  const from = before.toString().length;
  const to = from + range.toString().length;
  const text = start.textContent || '';
  const protectedText = /(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?!\d)|(?:https?:\/\/|www\.)[^\s<>，。！？；：、”’》】）]+|`+[^\n]*(?:`+|$)|\[[^\n]*\]\([^\n]*\)/gi;
  for (const match of text.matchAll(protectedText)) {
    if (match.index < to && match.index + match[0].length > from) return false;
  }
  return true;
}
