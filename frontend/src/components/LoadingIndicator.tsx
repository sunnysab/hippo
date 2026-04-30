import { useI18n } from '../i18n';

interface LoadingIndicatorProps {
  isLoading: boolean;
  id?: string;
}

export function LoadingIndicator({ isLoading, id }: LoadingIndicatorProps) {
  const { t } = useI18n();
  return (
    <div
      className={`loading-inline loading-center${isLoading ? '' : ' is-hidden'}`}
      id={id}
      role="status"
      aria-live="polite"
    >
      <span className="loading-spinner" aria-hidden="true"></span>
      <span>{t('common.loading', 'Loading...')}</span>
    </div>
  );
}
