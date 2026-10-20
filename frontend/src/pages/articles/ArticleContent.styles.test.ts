import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const articleStyles = readFileSync('src/styles/articles.css', 'utf8');
const commonStyles = readFileSync('src/styles/common.css', 'utf8');

describe('ArticleContent playback highlight styles', () => {
  let style: HTMLStyleElement;
  let reader: HTMLDivElement;

  beforeEach(() => {
    style = document.createElement('style');
    style.textContent = commonStyles + articleStyles;
    document.head.append(style);
    reader = document.createElement('div');
    reader.className = 'reader';
    document.body.append(reader);
  });

  afterEach(() => {
    style.remove();
    reader.remove();
  });

  it('keeps a short paint-only transition when the playing class is added and removed', () => {
    const paragraph = document.createElement('p');
    reader.append(paragraph);

    for (const className of ['', 'is-playing', '']) {
      paragraph.className = className;
      const computed = getComputedStyle(paragraph);
      expect(computed.transition).toBe('background-color 0.2s ease-out');
      expect(computed.borderRadius).toBe('var(--radius-sm)');
    }
  });

  it('uses the existing reduced-motion override for the paragraph transition', () => {
    // jsdom does not evaluate media queries; inspect the shared override instead.
    const reduced = [...style.sheet!.cssRules].find(
      (rule): rule is CSSMediaRule => rule instanceof CSSMediaRule
        && rule.conditionText === '(prefers-reduced-motion: reduce)',
    )!;
    const universal = [...reduced.cssRules].find(
      (rule): rule is CSSStyleRule => rule instanceof CSSStyleRule
        && rule.selectorText.split(',').some((selector) => selector.trim() === '*'),
    )!;

    expect(universal.style.getPropertyValue('transition-duration')).toBe('0.01ms');
    // jsdom's CSSOM drops !important inside media rules.
    expect(commonStyles).toContain('transition-duration: 0.01ms !important;');
  });
});
