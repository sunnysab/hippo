import type { ReactNode } from 'react';
import { useI18n } from '../../i18n';
import { parseWechatUrl } from '../../utils/markdown';
import type { ArticleReference } from './useArticleReferences';

interface ArticleReferenceLinkProps {
  href: string;
  label: ReactNode[];
  reference?: ArticleReference;
  failed: boolean;
  onRetry: (url: string) => void;
}

export function ArticleReferenceLink({ href, label, reference, failed, onRetry }: ArticleReferenceLinkProps) {
  const { t } = useI18n();
  if (!parseWechatUrl(href)) {
    return <a href={href} target='_blank' rel='noopener noreferrer'>{label}</a>;
  }
  const ready = reference?.status === 'ready' && reference.article_id !== null;
  const bare = label.length === 1 && typeof label[0] === 'string' && Boolean(parseWechatUrl(label[0]));
  const title = bare ? reference?.title || t('articles.reference.article', 'WeChat article') : label;
  const retryable = failed || reference?.status === 'failed';
  const status = retryable ? t('articles.reference.failed', 'Fetch failed')
    : reference?.status === 'pending' ? t('articles.reference.pending', 'Queued')
      : reference?.status === 'processing' ? t('articles.reference.processing', 'Fetching…')
        : ready ? t('articles.reference.ready', 'Archived') : '';

  return (
    <span className='reader-reference'>
      <a
        href={ready ? `#/articles?article=${reference.article_id}` : href}
        target={ready ? undefined : '_blank'}
        rel={ready ? undefined : 'noopener noreferrer'}
        title={reference?.title || undefined}
      >{title}</a>
      {status ? <small className='reader-reference-status'>{status}</small> : null}
      {retryable ? <button type='button' className='reader-reference-retry' onClick={() => onRetry(href)}>
        {t('articles.reference.retry', 'Retry')}
      </button> : null}
      {ready ? <a className='reader-reference-original' href={reference.canonical_url || href} target='_blank'
        rel='noopener noreferrer' aria-label={t('articles.reference.original', 'Open original')}
        title={t('articles.reference.original', 'Open original')}>↗</a> : null}
    </span>
  );
}
