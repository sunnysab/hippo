import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ContentKindBadge } from './ContentKindBadge';

describe('ContentKindBadge', () => {
  it('names the kind in platform-neutral words', () => {
    render(<ContentKindBadge kind='video' />);

    expect(screen.getByText('视频')).toBeTruthy();
  });

  it('gives each kind its own tone class', () => {
    const { container } = render(<ContentKindBadge kind='audio' />);

    expect(container.querySelector('.item-show-type-audio')).not.toBeNull();
  });

  it('renders nothing for a kind it does not know', () => {
    // 一个写着「未知」的徽章等于什么也没说。
    const { container } = render(<ContentKindBadge kind='hologram' />);

    expect(container.querySelector('.item-show-type-badge')).toBeNull();
  });

  it('renders nothing when the API did not send one', () => {
    const { container } = render(<ContentKindBadge kind={null} />);

    expect(container.querySelector('.item-show-type-badge')).toBeNull();
  });
});
