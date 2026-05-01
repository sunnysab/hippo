import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { I18nProvider } from '../../i18n';
import { ItemShowTypeBadge } from './ItemShowTypeBadge';

describe('ItemShowTypeBadge', () => {
  it('renders the translated badge without using HTML strings', () => {
    render(
      <I18nProvider>
        <ItemShowTypeBadge value={5} compact />
      </I18nProvider>,
    );

    expect(screen.getByText('视频分享')).toBeTruthy();
  });

  it('renders nothing for unknown values', () => {
    const view = render(
      <I18nProvider>
        <ItemShowTypeBadge value={999} />
      </I18nProvider>,
    );

    expect(view.container.textContent).toBe('');
  });
});
