import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { applyHighlights, buildTextMap, locate, type Anchor, type LocatedAnchor } from './anchor';

export interface Annotation {
  id: number;
  article_id: number;
  quote: string;
  prefix: string;
  suffix: string;
  note: string;
  color: string;
  created_at: string | null;
}

export interface StaleAnnotation extends Annotation {
  reason: string;
}

interface UseHighlightsOptions {
  articleId: number | null;
  containerRef: React.RefObject<HTMLElement | null>;
  /** Bumped whenever the rendered content changes, so marks can be re-applied. */
  contentKey: string;
}

export function useHighlights({ articleId, containerRef, contentKey }: UseHighlightsOptions) {
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [stale, setStale] = useState<StaleAnnotation[]>([]);

  // Fetch in the effect rather than through a shared callback: the state write
  // then happens after an await, which keeps the effect from cascading renders.
  useEffect(() => {
    if (!articleId) return;
    let cancelled = false;
    void (async () => {
      try {
        const payload = await apiGet(`/api/article/${articleId}/annotation`);
        // One request for the whole article; no per-highlight fetch.
        if (!cancelled) setAnnotations((payload.items as Annotation[]) ?? []);
      } catch {
        if (!cancelled) setAnnotations([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [articleId]);

  // Re-apply after every content change: React replaces the nodes, which drops
  // the existing <mark> wrappers.
  //
  // The whole body runs in a microtask: reading layout and writing the stale
  // list are both side effects, and doing that synchronously inside the effect
  // makes every article switch cascade a re-render.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    if (annotations.length === 0) {
      void Promise.resolve().then(() => setStale([]));
      return;
    }

    void Promise.resolve().then(() => {
      // Rebuild from the source text, otherwise previously inserted <mark>
      // elements would be counted as article text.
      const clean = container.cloneNode(true) as HTMLElement;
      clean.querySelectorAll('mark.hippo-highlight').forEach((mark) => {
        mark.replaceWith(...Array.from(mark.childNodes));
      });
      const map = buildTextMap(clean);

      const located: { anchor: Anchor; located: LocatedAnchor; id: number }[] = [];
      const missing: StaleAnnotation[] = [];
      for (const annotation of annotations) {
        const found = locate(map, annotation);
        if (found) {
          located.push({ anchor: annotation, located: found, id: annotation.id });
        } else {
          missing.push({ ...annotation, reason: 'stale' });
        }
      }

      // Clear whatever is rendered, then re-apply against a fresh map of the
      // live DOM.
      container.querySelectorAll('mark.hippo-highlight').forEach((mark) => {
        mark.replaceWith(...Array.from(mark.childNodes));
      });
      const liveMap = buildTextMap(container);
      applyHighlights(container, liveMap, located);
      setStale(missing);
    });
  }, [annotations, containerRef, contentKey]);

  const create = useCallback(
    async (anchor: Anchor, color = 'default') => {
      if (!articleId) return null;
      const created = (await apiSend(`/api/article/${articleId}/annotation`, 'POST', {
        ...anchor,
        color,
      })) as unknown as Annotation;
      setAnnotations((current) => [...current, created]);
      return created;
    },
    [articleId],
  );

  const remove = useCallback(
    async (annotationId: number) => {
      if (!articleId) return;
      await apiSend(`/api/article/${articleId}/annotation/${annotationId}`, 'DELETE', {});
      setAnnotations((current) => current.filter((item) => item.id !== annotationId));
    },
    [articleId],
  );

  return { annotations, stale, create, remove };
}
