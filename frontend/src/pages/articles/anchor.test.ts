import { describe, expect, it } from 'vitest';
import {
  applyHighlights,
  buildTextMap,
  locate,
  rangeFor,
  resolveOffset,
  selectionToSelector,
  type Anchor,
} from './anchor';

/**
 * Build a container whose text is the concatenation of `parts`, each part its
 * own text node. Two parts per paragraph is what the reader actually produces.
 */
function container(...parts: string[]): HTMLDivElement {
  const root = document.createElement('div');
  for (const part of parts) {
    const p = document.createElement('p');
    p.appendChild(document.createTextNode(part));
    root.appendChild(p);
  }
  return root;
}

const anchor = (quote: string, prefix = '', suffix = ''): Anchor => ({ quote, prefix, suffix });

describe('buildTextMap', () => {
  it('concatenates text nodes in document order', () => {
    const root = container('first', 'second', 'third');
    const map = buildTextMap(root);
    expect(map.text).toBe('firstsecondthird');
    expect(map.slices).toHaveLength(3);
    expect(map.slices[1]).toMatchObject({ start: 5, end: 11 });
  });

  it('resolves an offset back to its node', () => {
    const root = container('abc', 'def');
    const map = buildTextMap(root);
    const point = resolveOffset(map, 4);
    expect(point?.node.data).toBe('def');
    expect(point?.offset).toBe(1);
  });
});

describe('locate', () => {
  it('finds a unique match', () => {
    const root = container('hello ', 'brave new world');
    const map = buildTextMap(root);

    const found = locate(map, anchor('brave new world'));

    expect(found).not.toBeNull();
    expect(found?.start).toBe(6);
    expect(found?.end).toBe(21);
  });

  it('disambiguates repeated quotes using the surrounding context', () => {
    // "note" appears twice; only the second is preceded by "second ".
    const root = container('first note', 'second note');
    const map = buildTextMap(root);

    const found = locate(map, anchor('note', 'second ', ''));

    expect(found?.start).toBe(17);
    expect(found?.score).toBeGreaterThan(0);
  });

  it('prefers a later occurrence when the prefix only matches there', () => {
    const root = container('alpha beta alpha ');
    const map = buildTextMap(root);

    const found = locate(map, anchor('alpha', 'beta ', ''));

    expect(found?.start).toBe(11);
  });

  it('returns null once the original text has changed', () => {
    const root = container('the article was rewritten entirely');
    const map = buildTextMap(root);

    expect(locate(map, anchor('the original sentence'))).toBeNull();
  });

  it('returns null for an empty quote', () => {
    const map = buildTextMap(container('anything'));
    expect(locate(map, anchor(''))).toBeNull();
  });
});

describe('rangeFor', () => {
  it('spans two text nodes for a cross-node anchor', () => {
    const root = container('hello ', 'brave new world');
    const map = buildTextMap(root);

    const found = locate(map, anchor('o brave'));
    const range = rangeFor(map, found!);

    expect(range).not.toBeNull();
    expect(range?.toString()).toBe('o brave');
  });

  it('covers only the quoted text when it sits inside one node', () => {
    const root = container('abcdefghij', 'klmnop');
    const map = buildTextMap(root);

    const range = rangeFor(map, locate(map, anchor('defg'))!);

    expect(range?.toString()).toBe('defg');
  });
});

describe('selectionToSelector', () => {
  it('round-trips a selection that spans two nodes', () => {
    const root = container('hello ', 'brave new world');
    const map = buildTextMap(root);

    const range = document.createRange();
    range.setStart(map.slices[0].node, 3);
    range.setEnd(map.slices[1].node, 5);

    const selector = selectionToSelector(root, range);
    expect(selector?.quote).toBe('lo brave');

    // Locating the result in the same text must land on the same place.
    const found = locate(buildTextMap(root), selector!);
    expect(found?.start).toBe(3);
    expect(found?.end).toBe(11);
  });

  it('returns null for a collapsed selection', () => {
    const root = container('hello');
    const map = buildTextMap(root);
    const range = document.createRange();
    range.setStart(map.slices[0].node, 2);
    range.setEnd(map.slices[0].node, 2);

    expect(selectionToSelector(root, range)).toBeNull();
  });

  it('returns null for whitespace-only selections', () => {
    const root = container('hello   world');
    const range = document.createRange();
    range.setStart(root.querySelector('p')!.firstChild!, 5);
    range.setEnd(root.querySelector('p')!.firstChild!, 8);

    expect(selectionToSelector(root, range)).toBeNull();
  });

  it('captures context on both sides', () => {
    const root = container('0123456789abcdefghij');
    const range = document.createRange();
    const node = root.querySelector('p')!.firstChild!;
    range.setStart(node, 8);
    range.setEnd(node, 12);

    const selector = selectionToSelector(root, range);

    expect(selector?.quote).toBe('89ab');
    expect(selector?.prefix).toBe('01234567');
    expect(selector?.suffix).toBe('cdefghij');
  });
});

describe('applyHighlights', () => {
  it('wraps the matched text in a mark element', () => {
    const root = container('hello ', 'brave new world');
    const map = buildTextMap(root);
    const found = locate(map, anchor('brave new world'))!;

    const applied = applyHighlights(root, map, [{ anchor: found, located: found, id: 7 }]);

    expect(applied).toBe(1);
    const mark = root.querySelector('mark.hippo-highlight');
    expect(mark?.textContent).toBe('brave new world');
    expect(mark?.getAttribute('data-annotation-id')).toBe('7');
  });
});
