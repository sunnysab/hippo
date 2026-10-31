import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useI18n } from '../../i18n';
import { type Anchor } from './anchor';
import { eligibleSelection, rawSelection } from './corrections';

interface HighlightToolbarProps {
  containerRef: React.RefObject<HTMLElement | null>;
  onCreate: (anchor: Anchor) => Promise<unknown>;
  onCorrect: (source: string) => void;
  contentKey: string;
}

interface ToolbarState {
  anchor: Anchor;
  x: number;
  y: number;
  correctable: boolean;
  contentKey: string;
}

/**
 * Floating "highlight" action over a non-empty selection.
 *
 * Deliberately mouse-only: on touch devices the selection handles cover the
 * same area, so the toolbar would sit under the user's finger.
 */
export function HighlightToolbar({ containerRef, onCreate, onCorrect, contentKey }: HighlightToolbarProps) {
  const { t } = useI18n();
  const [state, setState] = useState<ToolbarState | null>(null);
  const [saving, setSaving] = useState(false);
  const toolbarRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const toolbar = toolbarRef.current;
    if (!toolbar || !state) return;
    const rect = toolbar.getBoundingClientRect();
    const dx = rect.left < 8 ? 8 - rect.left : rect.right > window.innerWidth - 8 ? window.innerWidth - 8 - rect.right : 0;
    const dy = rect.top < 8 ? 8 - rect.top : rect.bottom > window.innerHeight - 8 ? window.innerHeight - 8 - rect.bottom : 0;
    toolbar.style.left = `${state.x + dx}px`;
    toolbar.style.top = `${state.y + dy}px`;
  }, [state]);

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
      const anchor = rawSelection(container, range);
      if (!anchor) {
        dismiss();
        return;
      }
      const rect = range.getBoundingClientRect();
      setState({ anchor, x: rect.left + rect.width / 2, y: rect.top, correctable: eligibleSelection(range), contentKey });
    };

    const onMouseDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest('.highlight-toolbar')) return;
      dismiss();
    };

    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') dismiss(); };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.shiftKey || event.key === 'Shift' || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a')) onMouseUp();
    };
    document.addEventListener('mouseup', onMouseUp);
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('keyup', onKeyUp);
    document.addEventListener('mousedown', onMouseDown);
    document.addEventListener('scroll', dismiss, true);
    return () => {
      document.removeEventListener('mouseup', onMouseUp);
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('keyup', onKeyUp);
      document.removeEventListener('mousedown', onMouseDown);
      document.removeEventListener('scroll', dismiss, true);
    };
  }, [containerRef, dismiss, contentKey]);

  if (!state || state.contentKey !== contentKey) return null;

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
      ref={toolbarRef}
      className="highlight-toolbar"
      style={{ left: `${state.x}px`, top: `${state.y}px` }}
      role="toolbar"
    >
      <button className="btn" type="button" disabled={saving} onClick={() => void save()}>
        {t('articles.highlight', '划线')}
      </button>
      {state.correctable ? <button className="btn" type="button" onClick={() => { onCorrect(state.anchor.quote); window.getSelection()?.removeAllRanges(); dismiss(); }}>{t('articles.addCorrection', 'Add correction')}</button> : null}
    </div>
  );
}
