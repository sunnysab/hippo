import { describe, expect, it } from 'vitest';
import { SOURCE_META, getSourceLabel, getSourceTone } from './source';

const t = (key: string, fallback?: string) => fallback || key;

describe('source metadata', () => {
  it('labels the sources the backend can return today', () => {
    expect(getSourceLabel('weixin', t)).toBe('微信公众号');
    expect(getSourceLabel('bilibili', t)).toBe('哔哩哔哩');
  });

  it('shows an unknown source instead of hiding it', () => {
    expect(getSourceLabel('some-new-source', t)).toBe('some-new-source');
    expect(getSourceTone('some-new-source')).toBe('regular');
  });

  it('renders nothing when the field is absent', () => {
    expect(getSourceLabel(null, t)).toBe('');
    expect(getSourceLabel(undefined, t)).toBe('');
  });

  it('keeps every declared tone in the badge palette', () => {
    const tones = new Set(Object.values(SOURCE_META).map((meta) => meta.tone));
    expect([...tones].every((tone) => !tone.includes(' '))).toBe(true);
  });
});
