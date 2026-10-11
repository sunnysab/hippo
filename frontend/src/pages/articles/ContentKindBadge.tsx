import { CONTENT_KIND_META } from '../../utils/constants';
import { useI18n } from '../../i18n';

interface ContentKindBadgeProps {
  /** `article` / `video` / `audio` / … — platform-neutral, see CONTENT_KIND_META. */
  kind: string | null | undefined;
  compact?: boolean;
  className?: string;
}

/** Nothing is rendered for an unknown kind: a badge that says "unknown" says nothing. */
export function ContentKindBadge({ kind, compact = false, className = '' }: ContentKindBadgeProps) {
  const { t } = useI18n();

  const meta = kind ? CONTENT_KIND_META[kind] : undefined;
  if (!meta) return null;

  const compactClass = compact ? ' item-show-type-badge-compact' : '';
  const extraClass = className ? ` ${className}` : '';

  return (
    <span className={`item-show-type-badge item-show-type-${meta.tone}${compactClass}${extraClass}`}>
      {t(meta.key, meta.fallback)}
    </span>
  );
}
