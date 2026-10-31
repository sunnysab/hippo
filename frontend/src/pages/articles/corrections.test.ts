import { describe, expect, it } from 'vitest';
import { correctionMap, eligibleSelection, locateRaw, rawSelection, visibleCorrectionSpans } from './corrections';
import { buildContentBlockKeys } from './contentKeys';
import { applyHighlights, buildTextMap } from './anchor';

function sample() {
  const root = document.createElement('div');
  root.innerHTML = '<p>before <span data-asr-original="😀abXY!" data-asr-spans=\'[{"original_start":2,"original_end":4,"start":2,"end":3},{"original_start":4,"original_end":6,"start":3,"end":7}]\'>😀Qlong!</span> after</p>';
  return root;
}

describe('ASR correction anchors', () => {
  it('maps adjacent shorter and longer replacements in UTF-16 and expands partial selections', () => {
    const root = sample();
    const node = root.querySelector('span')!.firstChild!;
    const range = document.createRange();
    range.setStart(node, 4);
    range.setEnd(node, 5);
    expect(eligibleSelection(range)).toBe(true);
    const anchor = rawSelection(root, range)!;
    expect(anchor.quote).toBe('XY');
    const map = correctionMap(root);
    const found = locateRaw(map.raw, anchor)!;
    expect(map.toLive(found.start, false)).toBe(10);
    expect(map.toLive(found.end, true)).toBe(14);
  });

  it('locates repeated raw words by context and refuses ambiguous ties', () => {
    const root = document.createElement('div');
    root.textContent = 'first word second word';
    const map = correctionMap(root).raw;
    expect(locateRaw(map, { quote: 'word', prefix: 'second ', suffix: '' })?.start).toBe(18);
    expect(locateRaw(map, { quote: 'word', prefix: '', suffix: '' })).toBeNull();
  });

  it('keeps block keys and raw anchor identity when switching display modes', () => {
    const original = { type: 'paragraph' as const, transcript_source: 'asr', original_text: '😀abXY!', text: '😀abXY!', correction_spans: [] };
    const corrected = { ...original, text: '😀Qlong!' };
    expect(buildContentBlockKeys([original])).toEqual(buildContentBlockKeys([corrected]));
    const root = sample();
    const correctedMap = correctionMap(root);
    const anchor = { quote: 'XY', prefix: 'before 😀ab', suffix: '! after' };
    const found = locateRaw(correctedMap.raw, anchor)!;
    const span = root.querySelector('span')!;
    span.textContent = span.dataset.asrOriginal!;
    span.dataset.asrSpans = '[]';
    const rawMap = correctionMap(root);
    expect(locateRaw(rawMap.raw, anchor)?.start).toBe(found.start);
    expect(rawMap.toLive(found.start, false)).toBe(found.start);
  });

  it('refuses ambiguous Markdown selections and old highlights instead of expanding them', () => {
    expect(visibleCorrectionSpans('a *word', 'a *word*', [{ original_start: 3, original_end: 7, start: 3, end: 8 }])).toBeNull();
    const root = document.createElement('div');
    root.innerHTML = '<span data-asr-original="a *word" data-asr-spans="null">a <em>word</em></span><p>after</p>';
    const range = document.createRange();
    range.selectNodeContents(root.querySelector('em')!);
    expect(rawSelection(root, range)).toBeNull();
    expect(eligibleSelection(range)).toBe(false);
    const projection = correctionMap(root);
    const anchor = { quote: 'word', prefix: 'a *', suffix: 'after' };
    const found = locateRaw(projection.raw, anchor)!;
    expect(projection.canMap(found.start, found.end, false)).toBe(false);
    expect(projection.canMap(7, 12, false)).toBe(true);
    const span = root.querySelector('span')!;
    span.textContent = 'a *word';
    span.dataset.asrSpans = '[]';
    const original = correctionMap(root);
    const restored = locateRaw(original.raw, anchor)!;
    expect(original.canMap(restored.start, restored.end, false)).toBe(true);
    expect(original.toLive(restored.start, false)).toBe(3);
    expect(original.toLive(restored.end, true)).toBe(7);
  });

  it('retains both identities when disjoint raw highlights project onto one replacement', () => {
    const root = document.createElement('div');
    root.innerHTML = '<span data-asr-original="abcd" data-asr-spans=\'[{"original_start":0,"original_end":4,"start":0,"end":1}]\'>Z</span>';
    const anchors = [{ quote: 'ab', prefix: '', suffix: 'cd' }, { quote: 'cd', prefix: 'ab', suffix: '' }];
    const apply = () => {
      const projection = correctionMap(root);
      const items = anchors.map((anchor, index) => {
        const found = locateRaw(projection.raw, anchor)!;
        return { anchor, id: index + 1, located: { ...found, start: projection.toLive(found.start, false), end: projection.toLive(found.end, true) } };
      });
      expect(applyHighlights(root, buildTextMap(root), items)).toBe(2);
    };
    expect(apply).not.toThrow();
    expect(root.querySelector('[data-annotation-id="1"]')?.textContent).toBe('Z');
    expect(root.querySelector('[data-annotation-id="2"]')?.textContent).toBe('Z');
    const span = root.querySelector('span')!;
    span.textContent = 'abcd';
    span.dataset.asrSpans = '[]';
    apply();
    expect(root.querySelector('[data-annotation-id="1"]')?.textContent).toBe('ab');
    expect(root.querySelector('[data-annotation-id="2"]')?.textContent).toBe('cd');
  });

  it('rejects selections intersecting links, code, or transcript time text', () => {
    const root = document.createElement('div');
    root.innerHTML = '<span data-asr-original="plain link code 12:34 end">plain <a href="https://example.com">link</a> <code>code</code> 12:34 end</span>';
    const span = root.firstElementChild!;
    const range = document.createRange();
    range.setStart(span.firstChild!, 0);
    range.setEnd(span.querySelector('a')!.firstChild!, 2);
    expect(eligibleSelection(range)).toBe(false);
    range.selectNodeContents(span.querySelector('code')!);
    expect(eligibleSelection(range)).toBe(false);
    const last = span.lastChild!;
    range.setStart(last, 1);
    range.setEnd(last, 6);
    expect(eligibleSelection(range)).toBe(false);
  });

  it('rejects title and cross-block selections', () => {
    const root = sample();
    const range = document.createRange();
    range.setStart(root.querySelector('p')!.firstChild!, 0);
    range.setEnd(root.querySelector('span')!.firstChild!, 2);
    expect(eligibleSelection(range)).toBe(false);
  });
});
