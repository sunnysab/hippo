import { useEffect } from 'react';
import { useGroupsState, useGroupsActions } from '../../store/groups';
import { AccountCard } from './AccountCard';
import { useI18n } from '../../i18n';
import { EmptyState } from '../../components/EmptyState';

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
      account.alias.toLowerCase().includes(normalizedQuery) ||
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
    window.addEventListener('hippo:refresh', handler);
    return () => window.removeEventListener('hippo:refresh', handler);
  }, [loadAccounts]);

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
          key={[
            account.biz,
            account.sync_mode || '',
            account.sync_recent_days ?? '',
            state.selectedGroupId ?? '',
          ].join(':')}
          account={account}
        />
      ))}
    </div>
  );
}
