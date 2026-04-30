import { createContext, useContext, useReducer, useCallback, type ReactNode, type Dispatch } from 'react';
import type { Group, Account } from './shared';
import { syncDefaults } from './shared';
import { apiGet } from '../api';

interface SearchResult {
  biz: string;
  nickname: string;
  alias: string;
  round_head_img: string;
  avatar_url: string;
  is_added: boolean;
}

interface GroupsState {
  groups: Group[];
  defaultGroupId: number | null;
  selectedGroupId: number | null;
  accounts: Account[];
  selectedAccounts: string[];
  searchResults: SearchResult[];
  searchPage: number;
  searchHasMore: boolean;
  searchLoading: boolean;
}

type GroupsAction =
  | { type: 'SET_GROUPS'; payload: Group[]; defaultGroupId: number | null }
  | { type: 'SELECT_GROUP'; groupId: number | null }
  | { type: 'SET_ACCOUNTS'; accounts: Account[] }
  | { type: 'UPDATE_ACCOUNT'; biz: string; patch: Partial<Account> }
  | { type: 'TOGGLE_SELECTED'; biz: string }
  | { type: 'SET_SELECTED_ALL'; bizList: string[] }
  | { type: 'CLEAR_SELECTED' }
  | { type: 'SET_SEARCH_RESULTS'; results: SearchResult[]; append: boolean }
  | { type: 'SET_SEARCH_LOADING'; loading: boolean }
  | { type: 'SET_SEARCH_PAGE'; page: number; hasMore: boolean };

const initialState: GroupsState = {
  groups: [],
  defaultGroupId: null,
  selectedGroupId: null,
  accounts: [],
  selectedAccounts: [],
  searchResults: [],
  searchPage: 1,
  searchHasMore: true,
  searchLoading: false,
};

function reducer(state: GroupsState, action: GroupsAction): GroupsState {
  switch (action.type) {
    case 'SET_GROUPS':
      return { ...state, groups: action.payload, defaultGroupId: action.defaultGroupId };
    case 'SELECT_GROUP':
      return { ...state, selectedGroupId: action.groupId };
    case 'SET_ACCOUNTS':
      return { ...state, accounts: action.accounts, selectedAccounts: [] };
    case 'UPDATE_ACCOUNT':
      return {
        ...state,
        accounts: state.accounts.map((a) =>
          a.biz === action.biz ? { ...a, ...action.patch } : a,
        ),
      };
    case 'TOGGLE_SELECTED': {
      const exists = state.selectedAccounts.includes(action.biz);
      return {
        ...state,
        selectedAccounts: exists
          ? state.selectedAccounts.filter((b) => b !== action.biz)
          : [...state.selectedAccounts, action.biz],
      };
    }
    case 'SET_SELECTED_ALL':
      return { ...state, selectedAccounts: action.bizList };
    case 'CLEAR_SELECTED':
      return { ...state, selectedAccounts: [] };
    case 'SET_SEARCH_RESULTS':
      return {
        ...state,
        searchResults: action.append
          ? [...state.searchResults, ...action.results]
          : action.results,
      };
    case 'SET_SEARCH_LOADING':
      return { ...state, searchLoading: action.loading };
    case 'SET_SEARCH_PAGE':
      return { ...state, searchPage: action.page, searchHasMore: action.hasMore };
    default:
      return state;
  }
}

const GroupsContext = createContext<{
  state: GroupsState;
  dispatch: Dispatch<GroupsAction>;
} | null>(null);

export function GroupsProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return (
    <GroupsContext.Provider value={{ state, dispatch }}>
      {children}
    </GroupsContext.Provider>
  );
}

export function useGroupsState() {
  const ctx = useContext(GroupsContext);
  if (!ctx) throw new Error('useGroupsState must be used within GroupsProvider');
  return ctx;
}

export function useGroupsActions() {
  const { dispatch } = useGroupsState();
  const { state } = useGroupsState();

  const loadGroups = useCallback(async () => {
    const payload = await apiGet('/api/group');
    const groups = (payload.groups || []) as Group[];
    const defaultId = payload.default_group_id as number | null;
    dispatch({ type: 'SET_GROUPS', payload: groups, defaultGroupId: defaultId });

    let nextGroup = state.selectedGroupId;
    if (nextGroup && !groups.some((g) => g.id === nextGroup)) {
      nextGroup = null;
    }
    if (!nextGroup && defaultId) {
      nextGroup = defaultId;
    }
    if (nextGroup !== state.selectedGroupId) {
      dispatch({ type: 'SELECT_GROUP', groupId: nextGroup });
    }
    return { groups, defaultGroupId: defaultId, nextGroup };
  }, [state.selectedGroupId]);

  const loadAccounts = useCallback(async () => {
    const groupId = state.selectedGroupId;
    const url = new URL('/api/account', window.location.origin);
    if (groupId) url.searchParams.set('group_id', String(groupId));
    url.searchParams.set('page_size', '100');
    const payload = await apiGet(url.pathname + url.search);
    const accounts = (payload.accounts || []) as Account[];
    dispatch({ type: 'SET_ACCOUNTS', accounts });
  }, [state.selectedGroupId]);

  return { loadGroups, loadAccounts };
}
