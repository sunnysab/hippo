import { useCallback, useEffect, useState } from 'react';
import { useI18n } from '../../i18n';
import { selectionToSelector, type Anchor } from './anchor';

interface HighlightToolbarProps {
  containerRef: React.RefObject<HTMLElement | null>;
  onCreate: (anchor: Anchor) => Promise<unknown>;
}

interface ToolbarState {
  anchor: Anchor;
  x: number;
  y: number;
}

/**
 * Floating "highlight" action over a non-empty selection.
 *
 * Deliberately mouse-only: on touch devices the selection handles cover the
 * same area, so the toolbar would sit under the user's finger.
 */
export function HighlightToolbar({ containerRef, onCreate }: HighlightToolbarProps) {
  const { t } = useI18n();
  const [state, setState] = useState<ToolbarState | null>(null);
  const [saving, setSaving] = useState(false);

  const dismiss = useCallback(() => setState(null), []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const onMouseUp = () => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
        dismiss();
        return;
      }
      const range = selection.getRangeAt(0);
      if (!container.contains(range.commonAncestorContainer)) {
        dismiss();
        return;
      }
      const anchor = selectionToSelector(container, range);
      if (!anchor) {
        dismiss();
        return;
      }
      const rect = range.getBoundingClientRect();
      setState({ anchor, x: rect.left + rect.width / 2, y: rect.top });
    };

    const onMouseDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest('.highlight-toolbar')) return;
      dismiss();
    };

    document.addEventListener('mouseup', onMouseUp);
    document.addEventListener('mousedown', onMouseDown);
    document.addEventListener('scroll', dismiss, true);
    return () => {
      document.removeEventListener('mouseup', onMouseUp);
      document.removeEventListener('mousedown', onMouseDown);
      document.removeEventListener('scroll', dismiss, true);
    };
  }, [containerRef, dismiss]);

  if (!state) return null;

  const save = async () => {
    setSaving(true);
    try {
      await onCreate(state.anchor);
      window.getSelection()?.removeAllRanges();
      dismiss();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="highlight-toolbar"
      style={{ left: `${state.x}px`, top: `${state.y}px` }}
      role="toolbar"
    >
      <button className="btn" type="button" disabled={saving} onClick={() => void save()}>
        {t('articles.highlight', '划线')}
      </button>
    </div>
  );
}
