import { useEffect, useRef, useState, type ReactNode } from 'react';

interface PopoverProps {
  /** Id of the panel, also used to derive the trigger id. */
  id: string;
  label: string;
  trigger: ReactNode;
  triggerClassName?: string;
  /** A node, or a function receiving the close callback for menu-style panels. */
  children: ReactNode | ((close: () => void) => ReactNode);
}

/**
 * A toolbar control that opens a panel anchored to itself.
 *
 * It owns the open state and the dismissal rules (outside press, Escape) so the
 * callers do not each repeat that bookkeeping. The panel is a hover-free,
 * flat surface like every other floating surface in the app.
 */
export function Popover({ id, label, trigger, triggerClassName = 'icon-btn', children }: PopoverProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (rootRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      triggerRef.current?.focus();
    };

    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div className='popover' ref={rootRef}>
      <button
        className={triggerClassName}
        id={`${id}-trigger`}
        type='button'
        ref={triggerRef}
        title={label}
        aria-label={label}
        aria-haspopup='true'
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
      >
        {trigger}
      </button>
      {open ? (
        <div className='popover-panel' id={id} role='group' aria-label={label}>
          {typeof children === 'function' ? children(() => setOpen(false)) : children}
        </div>
      ) : null}
    </div>
  );
}
