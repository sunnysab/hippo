import { useEffect, useRef } from 'react';

interface ContextMenuProps {
  id: string;
  isOpen: boolean;
  x: number;
  y: number;
  onClose: () => void;
  children: React.ReactNode;
}

export function ContextMenu({ id, isOpen, x, y, onClose, children }: ContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const keyHandler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    const scrollHandler = () => onClose();
    document.addEventListener('click', handler);
    document.addEventListener('keydown', keyHandler);
    window.addEventListener('scroll', scrollHandler, true);
    return () => {
      document.removeEventListener('click', handler);
      document.removeEventListener('keydown', keyHandler);
      window.removeEventListener('scroll', scrollHandler, true);
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      ref={menuRef}
      className="context-menu"
      id={id}
      style={{ left: x, top: y }}
    >
      {children}
    </div>
  );
}
