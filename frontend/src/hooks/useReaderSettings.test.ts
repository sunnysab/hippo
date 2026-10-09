import { describe, expect, it } from 'vitest';
import {
  READER_LETTER_RANGE,
  READER_WIDTH_RANGE,
  normalizeReaderConfig,
  type ReaderConfig,
} from './useReaderSettings';

const config = (patch: Partial<ReaderConfig>): ReaderConfig => ({
  width: '760',
  font: '17',
  lineHeight: '1.8',
  letter: '0.2',
  serif: false,
  hideSmall: false,
  ...patch,
});

describe('normalizeReaderConfig', () => {
  it('moves the previous default width, which was never a deliberate choice', () => {
    expect(normalizeReaderConfig(config({ width: '860' })).width).toBe('760');
  });

  it('keeps a width the user picked inside the range', () => {
    expect(normalizeReaderConfig(config({ width: '700' })).width).toBe('700');
  });

  it('clamps stored values the sliders can no longer reach', () => {
    expect(normalizeReaderConfig(config({ width: '1500', letter: '4' }))).toMatchObject({
      width: String(READER_WIDTH_RANGE.max),
      letter: String(READER_LETTER_RANGE.max),
    });
  });

  it('falls back when a stored value is not a number', () => {
    expect(normalizeReaderConfig(config({ width: 'wide', letter: 'narrow' }))).toMatchObject({
      width: '760',
      letter: '0.2',
    });
  });
});
