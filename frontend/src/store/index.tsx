import { type ReactNode } from 'react';
import { GroupsProvider } from './groups';
import { ArticlesProvider } from './articles';
import { SettingsProvider } from './settings';

export function StoreProvider({ children }: { children: ReactNode }) {
  return (
    <GroupsProvider>
      <ArticlesProvider>
        <SettingsProvider>
          {children}
        </SettingsProvider>
      </ArticlesProvider>
    </GroupsProvider>
  );
}
