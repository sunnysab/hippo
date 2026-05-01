import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { I18nProvider, useI18n } from './index';

const tRefs: Array<(key: string, fallback?: string) => string> = [];

function Probe({ marker }: { marker: string }) {
  const { t } = useI18n();
  tRefs.push(t);
  return <span>{marker}</span>;
}

describe('I18nProvider', () => {
  it('keeps the same translator function across rerenders', () => {
    tRefs.length = 0;

    const view = render(
      <I18nProvider>
        <Probe marker="first" />
      </I18nProvider>,
    );

    view.rerender(
      <I18nProvider>
        <Probe marker="second" />
      </I18nProvider>,
    );

    expect(tRefs).toHaveLength(2);
    expect(tRefs[0]).toBe(tRefs[1]);
  });
});
