import { useEffect, useCallback } from 'react';
import { useGroupsState, useGroupsActions } from '../../store/groups';
import { AccountCard } from './AccountCard';
import { useI18n } from '../../i18n';
import { EmptyState } from '../../components/EmptyState';

export function AccountCardGrid() {
  const { state } = useGroupsState();
  const { loadAccounts } = useGroupsActions();
  const { t } = useI18n();

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

  if (!state.accounts.length) {
    return (
      <div className="card-grid" id="account-list">
        <EmptyState message={t('accounts.empty', 'No accounts found.')} />
      </div>
    );
  }

  return (
    <div className="card-grid" id="account-list">
      {state.accounts.map((account) => (
        <AccountCard key={account.biz} account={account} />
      ))}
    </div>
  );
}
