import { createRef } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../../i18n';
import { HighlightToolbar } from './HighlightToolbar';

describe('HighlightToolbar keyboard selection', () => {
  it('opens for keyboard selection and dismisses on Escape or content change', () => {
    const root = document.createElement('div');
    root.innerHTML = '<span data-asr-original="wrong" data-asr-spans="[]">wrong</span>';
    document.body.append(root);
    const ref = createRef<HTMLDivElement>();
    ref.current = root;
    const selection = window.getSelection()!;
    const range = document.createRange();
    range.selectNodeContents(root.firstElementChild!);
    range.getBoundingClientRect = () => ({ left: 10, top: 40, width: 40, height: 10, right: 50, bottom: 50, x: 10, y: 40, toJSON: () => ({}) });
    selection.removeAllRanges(); selection.addRange(range);
    const renderToolbar = (key: string) => <I18nProvider><HighlightToolbar containerRef={ref} contentKey={key} onCreate={vi.fn()} onCorrect={vi.fn()} /></I18nProvider>;
    const view = render(renderToolbar('first'));
    fireEvent.keyUp(document, { key: 'Shift' });
    expect(screen.getByRole('button', { name: '添加纠错' })).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('button', { name: '添加纠错' })).toBeNull();
    fireEvent.keyUp(document, { key: 'Shift' });
    view.rerender(renderToolbar('second'));
    expect(screen.queryByRole('button', { name: '添加纠错' })).toBeNull();
    selection.removeAllRanges();
    root.remove();
  });
});
