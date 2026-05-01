import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderInlineNodes } from './markdown';

function Probe({ text }: { text: string }) {
  return <div>{renderInlineNodes(text)}</div>;
}

describe('renderInlineNodes', () => {
  it('does not render unsafe javascript links as anchors', () => {
    render(<Probe text={'Click [me](javascript:alert(1))'} />);

    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText('Click [me](javascript:alert(1))')).toBeTruthy();
  });

  it('renders safe links and inline emphasis as React nodes', () => {
    render(<Probe text={'Read **this** [post](https://example.com)'} />);

    const link = screen.getByRole('link', { name: 'post' });
    expect(link.getAttribute('href')).toBe('https://example.com/');
    expect(screen.getByText('this').tagName).toBe('STRONG');
  });
});
