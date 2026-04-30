import { createContext, useContext, useReducer, type ReactNode, type Dispatch } from 'react';

export interface SyncStatus {
  status: string;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_error: string | null;
  history: Array<Record<string, unknown>>;
}

export interface SyncSettings {
  enabled: boolean;
  interval_minutes: number;
  window_start_hour: number;
  window_end_hour: number;
  sleep_seconds: number;
  skip_minutes: number;
  download_content: boolean;
  download_images: boolean;
  article_exclude_keywords: string;
  alert_enabled: boolean;
  alert_email: string;
  email?: Record<string, unknown>;
}

export interface SyncTaskAccount {
  biz: string;
  nickname?: string;
  status: string;
  saved: number;
  page_count: number;
  article_current: number | null;
  article_total: number | null;
  last_article?: { article_id?: string; title?: string };
  phase?: string;
  updated_at: string | null;
  skip_reason: string;
  error?: string;
}

export interface SyncTask {
  task_id: string;
  status: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  trigger_type: string;
  phase?: string;
  last_log: string | null;
  accounts_total: number;
  accounts_done: number;
  current_account?: { biz: string; nickname?: string };
  current_article?: { article_id?: string; title?: string };
  accounts: SyncTaskAccount[];
}

export interface LoginStatus {
  status: string;
  message: string;
  qrcode_url: string | null;
  last_login: Record<string, unknown> | null;
}

interface SettingsState {
  syncStatus: SyncStatus | null;
  syncSettings: SyncSettings | null;
  syncTasks: SyncTask[];
  loginStatus: LoginStatus | null;
  syncPollTimer: ReturnType<typeof setInterval> | null;
  loginPollTimer: ReturnType<typeof setInterval> | null;
}

type SettingsAction =
  | { type: 'SET_SYNC_STATUS'; payload: SyncStatus }
  | { type: 'SET_SYNC_SETTINGS'; payload: SyncSettings }
  | { type: 'SET_SYNC_TASKS'; tasks: SyncTask[] }
  | { type: 'SET_LOGIN_STATUS'; payload: LoginStatus };

const initialState: SettingsState = {
  syncStatus: null,
  syncSettings: null,
  syncTasks: [],
  loginStatus: null,
  syncPollTimer: null,
  loginPollTimer: null,
};

function reducer(state: SettingsState, action: SettingsAction): SettingsState {
  switch (action.type) {
    case 'SET_SYNC_STATUS':
      return { ...state, syncStatus: action.payload };
    case 'SET_SYNC_SETTINGS':
      return { ...state, syncSettings: action.payload };
    case 'SET_SYNC_TASKS':
      return { ...state, syncTasks: action.tasks };
    case 'SET_LOGIN_STATUS':
      return { ...state, loginStatus: action.payload };
    default:
      return state;
  }
}

const SettingsContext = createContext<{
  state: SettingsState;
  dispatch: Dispatch<SettingsAction>;
} | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return (
    <SettingsContext.Provider value={{ state, dispatch }}>
      {children}
    </SettingsContext.Provider>
  );
}

export function useSettingsState() {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error('useSettingsState must be used within SettingsProvider');
  return ctx;
}
