import { memo, useEffect, useRef } from 'react';
import { useI18n } from '../../i18n';
import { renderInline } from '../../utils/markdown';
import { EmptyState } from '../../components/EmptyState';
import type { ArticlePayload, ArticleContentBlock } from '../../store/articles';

interface ArticleContentProps {
  payload: ArticlePayload;
  hideSmall: boolean;
  onImageContextMenu: (detail: { imageId: number; x: number; y: number }) => void;
}

interface ContentBlockProps {
  block: ArticleContentBlock;
  images: ArticlePayload['images'];
  hideSmall: boolean;
  onImageContextMenu: (detail: { imageId: number; x: number; y: number }) => void;
}

const ArticleImageBlock = memo(function ArticleImageBlock({
  block,
  images,
  hideSmall,
  onImageContextMenu,
}: ContentBlockProps) {
  const figureRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const figure = figureRef.current;
    if (!figure) return;

    figure.style.display = '';
    if (!hideSmall) return;

    const img = figure.querySelector('img');
    if (!img) return;

    const onLoad = () => {
      const w = img.naturalWidth;
      const h = img.naturalHeight;
      const imageMeta = (images || []).find((item) => item.id === block.image_id);
      const isGif = imageMeta?.content_type && String(imageMeta.content_type).includes('gif');

      if (isGif) {
        figure.style.display = 'none';
        return;
      }

      if (w < 500 && h < 500) {
        const ratio = w / h;
        if (ratio < 1.2) {
          figure.style.display = 'none';
        }
      }
    };

    if (img.complete) {
      onLoad();
      return;
    }

    img.addEventListener('load', onLoad);
    return () => img.removeEventListener('load', onLoad);
  }, [block.image_id, hideSmall, images]);

  if (!block.image_id) return null;

  return (
    <figure ref={figureRef} data-image-id={String(block.image_id)}>
      <img
        src={`/api/image/${block.image_id}`}
        alt={block.alt || ''}
        loading='lazy'
        data-image-id={String(block.image_id)}
        onContextMenu={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onImageContextMenu({
            imageId: block.image_id!,
            x: event.clientX,
            y: event.clientY,
          });
        }}
      />
    </figure>
  );
});

const ContentBlock = memo(function ContentBlock({
  block,
  images,
  hideSmall,
  onImageContextMenu,
}: ContentBlockProps) {
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
    return (
      <ArticleImageBlock
        block={block}
        images={images}
        hideSmall={hideSmall}
        onImageContextMenu={onImageContextMenu}
      />
    );
  }

  return null;
});

export function ArticleContent({ payload, hideSmall, onImageContextMenu }: ArticleContentProps) {
  const { t } = useI18n();

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
        <ContentBlock
          key={i}
          block={block}
          images={payload.images}
          hideSmall={hideSmall}
          onImageContextMenu={onImageContextMenu}
        />
      ))}
    </>
  );
}
