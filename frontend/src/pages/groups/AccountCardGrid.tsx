import { useCallback, useEffect } from 'react';
import { useGroupsState, useGroupsActions } from '../../store/groups';
import { AccountCard } from './AccountCard';
import { useI18n } from '../../i18n';
import { EmptyState } from '../../components/EmptyState';
import { hasAccountWork } from '../../utils/sync';
import { usePolling } from '../../hooks/usePolling';
import { onRefresh } from '../../utils/events';

interface AccountCardGridProps {
  query: string;
}

export function AccountCardGrid({ query }: AccountCardGridProps) {
  const { state } = useGroupsState();
  const { loadAccounts } = useGroupsActions();
  const { t } = useI18n();
  const normalizedQuery = query.trim().toLowerCase();
  const accounts = normalizedQuery
    ? state.accounts.filter((account) => (
      account.nickname.toLowerCase().includes(normalizedQuery) ||
      (account.alias || '').toLowerCase().includes(normalizedQuery) ||
      account.biz.toLowerCase().includes(normalizedQuery)
    ))
    : state.accounts;

  useEffect(() => {
    if (state.selectedGroupId) {
      void loadAccounts();
    }
  }, [state.selectedGroupId, loadAccounts]);

  useEffect(() => {
    const handler = () => {
      void loadAccounts();
    };
    return onRefresh(handler);
  }, [loadAccounts]);

  const hasActiveBackfill = state.accounts.some((account) => account.sync ? hasAccountWork(account.sync) : account.backfill_running || account.backfill_pending);

  const refreshAccounts = useCallback(async (signal: AbortSignal) => {
    await loadAccounts(true, signal);
  }, [loadAccounts]);
  usePolling(refreshAccounts, hasActiveBackfill ? 5000 : 30000, false);

  if (!accounts.length) {
    return (
      <div className="card-grid" id="account-list">
        <EmptyState message={t('accounts.empty', 'No accounts found.')} />
      </div>
    );
  }

  return (
    <div className="card-grid" id="account-list">
      {accounts.map((account) => (
        <AccountCard
          key={account.biz}
          account={account}
        />
      ))}
    </div>
  );
}
