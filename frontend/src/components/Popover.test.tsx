import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Popover } from './Popover';

const renderPopover = () => render(
  <Popover id='demo' label='设置' trigger={<span aria-hidden='true'>Aa</span>}>
    {(close) => <button onClick={close}>收起</button>}
  </Popover>,
);

describe('Popover', () => {
  it('opens the panel from the trigger and reports its state', () => {
    renderPopover();
    const trigger = screen.getByLabelText('设置');

    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(trigger);

    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('group', { name: '设置' })).toBeTruthy();
  });

  it('closes when a press lands outside', () => {
    renderPopover();
    fireEvent.click(screen.getByLabelText('设置'));

    fireEvent.pointerDown(document.body);

    expect(screen.queryByRole('group', { name: '设置' })).toBeNull();
  });

  it('keeps the panel open for presses inside it', () => {
    renderPopover();
    fireEvent.click(screen.getByLabelText('设置'));

    fireEvent.pointerDown(screen.getByRole('group', { name: '设置' }));

    expect(screen.getByRole('group', { name: '设置' })).toBeTruthy();
  });

  it('closes on Escape and hands focus back to the trigger', () => {
    renderPopover();
    const trigger = screen.getByLabelText('设置');
    fireEvent.click(trigger);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('group', { name: '设置' })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('gives the panel a close handle when it holds a menu', () => {
    const onClose = vi.fn();
    render(
      <Popover id='menu' label='复制' trigger={<span />} >
        {(close) => <button onClick={() => { onClose(); close(); }}>纯文本</button>}
      </Popover>,
    );
    fireEvent.click(screen.getByLabelText('复制'));

    fireEvent.click(screen.getByText('纯文本'));

    expect(onClose).toHaveBeenCalled();
    expect(screen.queryByRole('group', { name: '复制' })).toBeNull();
  });
});
