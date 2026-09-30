/**
 * Text anchoring for highlights.
 *
 * Highlights survive re-rendering by quoting the selected text plus a little
 * surrounding context, never by character offset: the reader rebuilds its DOM
 * from freshly fetched article HTML, and any offset taken today points at
 * different words tomorrow.
 *
 * The whole module is DOM-in / plain-object-out so it can be unit tested
 * without a browser layout engine.
 */

export interface TextMap {
  /** Concatenated text of every text node under the root, in document order. */
  text: string;
  /** Offsets of each text node within `text`, for mapping back to nodes. */
  slices: { node: Text; start: number; end: number }[];
}

export interface Anchor {
  quote: string;
  prefix: string;
  suffix: string;
}

export interface LocatedAnchor extends Anchor {
  start: number;
  end: number;
  /** Higher is a better match when the same quote appears more than once. */
  score: number;
}

/** Context kept on each side, matching the server-side cap. */
export const CONTEXT_LENGTH = 32;

/**
 * Walk every text node once and remember where each one sits in the
 * concatenated string.
 *
 * This is the part that is easy to get subtly wrong: the concatenation order
 * must be exactly document order, and block boundaries contribute no separator,
 * so offsets stay comparable across renders of the same content.
 */
export function buildTextMap(root: Node): TextMap {
  const slices: TextMap['slices'] = [];
  let text = '';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let current = walker.nextNode();
  while (current) {
    const node = current as Text;
    const value = node.data;
    if (value.length > 0) {
      slices.push({ node, start: text.length, end: text.length + value.length });
      text += value;
    }
    current = walker.nextNode();
  }
  return { text, slices };
}

/** Map a flat offset back to the node that contains it. */
export function resolveOffset(map: TextMap, offset: number): { node: Text; offset: number } | null {
  for (const slice of map.slices) {
    if (offset >= slice.start && offset <= slice.end) {
      return { node: slice.node, offset: offset - slice.start };
    }
  }
  return null;
}

/**
 * Score how well a candidate occurrence is surrounded by the expected context.
 *
 * Comparing characters outward from the quote means a partially re-rendered
 * neighbourhood still scores above a coincidental duplicate elsewhere.
 */
export function contextScore(text: string, start: number, end: number, anchor: Anchor): number {
  let score = 0;
  const prefix = anchor.prefix;
  for (let i = 1; i <= prefix.length; i += 1) {
    if (start - i < 0) break;
    if (text[start - i] !== prefix[prefix.length - i]) break;
    score += 1;
  }
  const suffix = anchor.suffix;
  for (let i = 0; i < suffix.length; i += 1) {
    if (end + i >= text.length) break;
    if (text[end + i] !== suffix[i]) break;
    score += 1;
  }
  return score;
}

/**
 * Find an anchor in the current text.
 *
 * Returns `null` when the quote is gone — the article was edited, or the
 * highlight was made on a different version. Callers surface those as stale
 * rather than guessing a position.
 */
export function locate(map: TextMap, anchor: Anchor): LocatedAnchor | null {
  const quote = anchor.quote;
  if (!quote) return null;

  let best: LocatedAnchor | null = null;
  let index = map.text.indexOf(quote);
  while (index !== -1) {
    const start = index;
    const end = index + quote.length;
    const score = contextScore(map.text, start, end, anchor);
    if (!best || score > best.score) {
      best = { ...anchor, start, end, score };
    }
    // A perfect context match cannot be beaten; stop scanning duplicates.
    if (score === anchor.prefix.length + anchor.suffix.length) break;
    index = map.text.indexOf(quote, index + 1);
  }
  return best;
}

/** Build a Range for a located anchor, spanning text nodes if needed. */
export function rangeFor(map: TextMap, located: LocatedAnchor): Range | null {
  const startPoint = resolveOffset(map, located.start);
  const endPoint = resolveOffset(map, located.end);
  if (!startPoint || !endPoint) return null;
  const range = document.createRange();
  range.setStart(startPoint.node, startPoint.offset);
  range.setEnd(endPoint.node, endPoint.offset);
  return range;
}

/**
 * Turn a DOM selection into an anchor.
 *
 * The selection may span several text nodes; its flat offsets are computed
 * through the same map the locate step uses, so the two agree.
 */
export function selectionToSelector(root: Node, range: Range): Anchor | null {
  const map = buildTextMap(root);
  const start = offsetInMap(map, range.startContainer, range.startOffset);
  const end = offsetInMap(map, range.endContainer, range.endOffset);
  if (start === null || end === null || end <= start) return null;

  const quote = map.text.slice(start, end);
  if (!quote.trim()) return null;
  return {
    quote,
    prefix: map.text.slice(Math.max(start - CONTEXT_LENGTH, 0), start),
    suffix: map.text.slice(end, end + CONTEXT_LENGTH),
  };
}

function offsetInMap(map: TextMap, container: Node, offset: number): number | null {
  for (const slice of map.slices) {
    if (slice.node === container) {
      return slice.start + offset;
    }
  }
  // The selection landed on an element (e.g. between two block nodes): find the
  // first text node at or after it.
  for (const slice of map.slices) {
    if (container.contains(slice.node)) {
      return slice.start;
    }
  }
  return null;
}

/**
 * Wrap matches in `<mark>` elements.
 *
 * Applied right-to-left so earlier offsets stay valid as later ones are
 * rewritten, and only over the nodes each anchor actually covers.
 */
export function applyHighlights(
  root: HTMLElement,
  map: TextMap,
  located: { anchor: Anchor; located: LocatedAnchor; id: number; stale?: boolean }[],
): number {
  let applied = 0;
  // Right to left: splitting a node changes nothing before the split point.
  for (const item of [...located].sort((a, b) => b.located.start - a.located.start)) {
    const range = rangeFor(map, item.located);
    if (!range) continue;
    const mark = document.createElement('mark');
    mark.className = 'hippo-highlight';
    mark.dataset.annotationId = String(item.id);
    try {
      // extractContents handles a range that starts and ends mid-node and one
      // that spans several nodes; surroundContents rejects the latter.
      mark.appendChild(range.extractContents());
      range.insertNode(mark);
      applied += 1;
    } catch {
      // A detached or inverted range: skip this one rather than abort the rest.
      continue;
    }
  }
  return applied;
}
