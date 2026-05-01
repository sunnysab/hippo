import { Component, type ReactNode } from 'react';
import { useI18n } from '../i18n';
import { EmptyState } from './EmptyState';

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = {
    hasError: false,
  };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch() {
    /* keep fallback rendering local and avoid crashing the whole tree */
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback;
    }

    return this.props.children;
  }
}

export function AppErrorBoundary({ children }: { children: ReactNode }) {
  const { t } = useI18n();

  return (
    <ErrorBoundary
      fallback={
        <div className="app-error" role="alert">
          <EmptyState message={t('common.unexpectedError', 'Unexpected error. Please refresh and try again.')} />
        </div>
      }
    >
      {children}
    </ErrorBoundary>
  );
}
