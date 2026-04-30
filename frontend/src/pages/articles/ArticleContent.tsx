import { useRef, useEffect } from 'react';
import { useI18n } from '../../i18n';
import { renderInline } from '../../utils/markdown';
import { EmptyState } from '../../components/EmptyState';
import type { ArticlePayload, ArticleContentBlock } from '../../store/articles';

interface ArticleContentProps {
  payload: ArticlePayload;
  hideSmall: boolean;
  onBlockImage: (imageId: number) => void;
}

function ContentBlock({ block, images, hideSmall }: {
  block: ArticleContentBlock;
  images: ArticlePayload['images'];
  hideSmall: boolean;
}) {
  const figureRef = useRef<HTMLElement>(null);

  if (block.type === 'paragraph') {
    return <p dangerouslySetInnerHTML={{ __html: renderInline(block.text || '') }} />;
  }

  if (block.type === 'heading') {
    const level = Math.min(Math.max(Number(block.level) || 2, 2), 4);
    const Tag = `h${level}` as const;
    // @ts-ignore
    return <Tag dangerouslySetInnerHTML={{ __html: renderInline(block.text || '') }} />;
  }

  if (block.type === 'image') {
    if (!block.image_id) return null;

    useEffect(() => {
      if (!hideSmall || !figureRef.current) return;
      const img = figureRef.current.querySelector('img');
      if (!img) return;

      const onLoad = () => {
        const w = img.naturalWidth;
        const h = img.naturalHeight;

        const imageMeta = (images || []).find((i) => i.id === block.image_id);
        const isGif = imageMeta?.content_type && String(imageMeta.content_type).includes('gif');

        if (isGif) {
          if (figureRef.current) figureRef.current.style.display = 'none';
          return;
        }

        if (w < 500 && h < 500) {
          const ratio = w / h;
          if (ratio < 1.2) {
            if (figureRef.current) figureRef.current.style.display = 'none';
          }
        }
      };

      img.addEventListener('load', onLoad);
      return () => img.removeEventListener('load', onLoad);
    }, [hideSmall, images, block.image_id]);

    return (
      <figure ref={figureRef} data-image-id={String(block.image_id)}>
        <img
          src={`/api/image/${block.image_id}`}
          alt={block.alt || ''}
          loading="lazy"
          data-image-id={String(block.image_id)}
          onContextMenu={(e) => {
            e.preventDefault();
            e.stopPropagation();
            window.dispatchEvent(new CustomEvent('hippo:image-context-menu', {
              detail: { imageId: block.image_id, x: e.clientX, y: e.clientY },
            }));
          }}
        />
      </figure>
    );
  }

  return null;
}

export function ArticleContent({ payload, hideSmall, onBlockImage }: ArticleContentProps) {
  const { t } = useI18n();

  // Listen for image context menu events
  useEffect(() => {
    const handler = (e: Event) => {
      const { imageId, x, y } = (e as CustomEvent).detail as { imageId: number; x: number; y: number };
      const menu = document.getElementById('article-image-context-menu');
      if (menu) {
        (menu as HTMLElement).dataset.imageId = String(imageId);
        menu.style.left = `${x}px`;
        menu.style.top = `${y}px`;
        menu.classList.remove('is-hidden');
      }
    };
    const clickHandler = (e: MouseEvent) => {
      const menu = document.getElementById('article-image-context-menu');
      if (menu && !menu.classList.contains('is-hidden') && !menu.contains(e.target as Node)) {
        menu.classList.add('is-hidden');
      }
    };
    window.addEventListener('hippo:image-context-menu', handler);
    document.addEventListener('click', clickHandler);
    return () => {
      window.removeEventListener('hippo:image-context-menu', handler);
      document.removeEventListener('click', clickHandler);
    };
  }, []);

  // Wire up the block image button
  useEffect(() => {
    const btn = document.getElementById('article-image-menu-block');
    if (!btn) return;
    const clickHandler = async () => {
      const menu = document.getElementById('article-image-context-menu');
      if (!menu) return;
      const imageId = Number((menu as HTMLElement).dataset.imageId || 0);
      if (!imageId) return;
      (btn as HTMLButtonElement).disabled = true;
      try {
        await onBlockImage(imageId);
        menu.classList.add('is-hidden');
      } finally {
        (btn as HTMLButtonElement).disabled = false;
      }
    };
    btn.addEventListener('click', clickHandler);
    return () => btn.removeEventListener('click', clickHandler);
  }, [onBlockImage]);

  const content = payload.content;
  const status = String(payload.content_status || '').trim().toLowerCase();

  if (!Array.isArray(content)) {
    return (
      <div className="reader">
        <EmptyState message={
          status === 'invalid'
            ? t('articles.contentInvalid', 'Failed to parse article content. Please try syncing again.')
            : t('articles.contentMissing', 'Article content is not available yet. Please wait for sync to finish or sync again.')
        } />
      </div>
    );
  }

  if (content.length === 0) {
    return (
      <div className="reader">
        <EmptyState message={t('articles.contentEmpty', 'This article has no content.')} />
      </div>
    );
  }

  return (
    <>
      {content.map((block, i) => (
        <ContentBlock key={i} block={block} images={payload.images} hideSmall={hideSmall} />
      ))}
    </>
  );
}
