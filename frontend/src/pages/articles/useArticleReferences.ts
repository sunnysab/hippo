import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import type { ArticlePayload } from '../../store/articles';

export interface ArticleReference {
  url: string;
  status: 'ready' | 'missing' | 'pending' | 'processing' | 'failed';
  article_id: number | null;
  title: string | null;
  canonical_url: string | null;
}

export function useArticleReferences(payload: ArticlePayload) {
  const key = `${payload.article.id}:${payload.content_updated_at ?? ''}`;
  const path = `/api/article/${payload.article.id}/reference`;
  const hasLinks = Boolean(payload.content?.some(block => block.type !== 'code' &&
    /(?:https?:)?\/\/mp\.weixin\.qq\.com\//i.test(`${block.text ?? ''} ${block.href ?? ''}`)));
  const [snapshot, setSnapshot] = useState<{ key: string; items: ArticleReference[]; failed: boolean } | null>(null);
  const [retry, setRetry] = useState<{ key: string; url: string; attempt: number } | null>(null);

  useEffect(() => {
    if (!hasLinks) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const options = { signal: controller.signal };
    const publish = (answer: Record<string, unknown>) => {
      const items = (answer.items ?? []) as ArticleReference[];
      if (!controller.signal.aborted) setSnapshot({ key, items, failed: false });
      return items;
    };
    const poll = async () => {
      try {
        const items = publish(await apiGet(path, options));
        if (!controller.signal.aborted && items.some(item => ['pending', 'processing'].includes(item.status))) {
          timer = setTimeout(() => { void poll(); }, 3000);
        }
      } catch {
        if (!controller.signal.aborted) setSnapshot(current => ({
          key, items: current?.key === key ? current.items : [], failed: true,
        }));
      }
    };
    const start = async () => {
      try {
        const initial = await apiGet(path, options);
        const items = (initial.items ?? []) as ArticleReference[];
        if (controller.signal.aborted) return;
        if (!items.length) { publish(initial); return; }
        // POST establishes direct-reference access even when every body is already stored.
        const result = publish(await apiSend(path, 'POST',
          retry?.key === key ? { retry_url: retry.url } : {}, options));
        if (!controller.signal.aborted && result.some(item => ['pending', 'processing'].includes(item.status))) {
          timer = setTimeout(() => { void poll(); }, 3000);
        }
      } catch {
        if (!controller.signal.aborted) setSnapshot(current => ({
          key, items: current?.key === key ? current.items : [], failed: true,
        }));
      }
    };
    void start();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [hasLinks, key, path, retry]);

  const references = useMemo(() => new Map(
    snapshot?.key === key ? snapshot.items.map(item => [item.url, item]) : [],
  ), [key, snapshot]);
  const retryReference = useCallback((url: string) => {
    setRetry(previous => ({ key, url, attempt: (previous?.attempt ?? 0) + 1 }));
  }, [key]);
  return {
    references,
    failed: snapshot?.key === key && snapshot.failed,
    retry: retryReference,
  };
}
