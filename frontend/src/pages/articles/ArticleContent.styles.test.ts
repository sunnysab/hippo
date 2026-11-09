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

describe('Player dock styles', () => {
  let style: HTMLStyleElement;

  beforeEach(() => {
    style = document.createElement('style');
    style.textContent = commonStyles + articleStyles;
    document.head.append(style);
  });

  afterEach(() => {
    style.remove();
  });

  it('floats the dock above the page but below the top bar and toasts', () => {
    const dock = document.createElement('div');
    dock.className = 'reader-dock';
    document.body.append(dock);
    const computed = getComputedStyle(dock);
    expect(computed.position).toBe('fixed');
    expect(Number(computed.zIndex)).toBeLessThan(30);
    expect(computed.animation).toContain('dock-in');
    dock.remove();
  });

  it('has a reduced-motion rule covering the dock entrance', () => {
    const reduced = [...style.sheet!.cssRules].find(
      (rule): rule is CSSMediaRule => rule instanceof CSSMediaRule
        && rule.conditionText === '(prefers-reduced-motion: reduce)',
    )!;
    expect(reduced).toBeTruthy();
    expect(commonStyles).toContain('animation-iteration-count: 1 !important;');
  });
});

describe('Mini player styles', () => {
  let style: HTMLStyleElement;

  beforeEach(() => {
    style = document.createElement('style');
    style.textContent = commonStyles + articleStyles;
    document.head.append(style);
  });

  afterEach(() => {
    style.remove();
  });

  it('floats the video above the page and below the top bar', () => {
    const frame = document.createElement('div');
    frame.className = 'reader-video is-pip';
    document.body.append(frame);
    const computed = getComputedStyle(frame);
    expect(computed.position).toBe('fixed');
    expect(Number(computed.zIndex)).toBeLessThan(30);
    expect(computed.animation).toContain('pip-in');
    frame.remove();
  });

  it('keeps the transcript height with a slot that replaces the frame in flow', () => {
    const slot = document.createElement('div');
    slot.className = 'reader-video-slot';
    document.body.append(slot);
    expect(getComputedStyle(slot).aspectRatio).toBe('16 / 9');
    slot.remove();
  });

  it('keeps the switches under the picture visible instead of clipping them', () => {
    const frame = document.createElement('div');
    frame.className = 'reader-video';
    const video = document.createElement('video');
    frame.append(video);
    document.body.append(frame);
    // 容器一旦自己定死比例，下面那行开关就落在 overflow 之外看不见了。
    expect(getComputedStyle(frame).aspectRatio).not.toBe('16 / 9');
    expect(getComputedStyle(video).aspectRatio).toBe('16 / 9');
    frame.remove();
  });

  it('fills the viewport with the page still behind it, above the top bar', () => {
    const frame = document.createElement('div');
    frame.className = 'reader-video is-theater';
    document.body.append(frame);
    const computed = getComputedStyle(frame);
    expect(computed.position).toBe('fixed');
    // 顶栏是 30：铺满的画面盖住它，而不是从它下面穿过去。
    expect(Number(computed.zIndex)).toBeGreaterThan(30);
    frame.remove();
  });

  it('keeps the drag grip out of the browser gesture that would scroll the page', () => {
    // jsdom's CSSOM drops touch-action, so the source is the only witness here.
    expect(articleStyles).toContain('touch-action: none;');
  });
});
