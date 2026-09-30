import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { apiGet, apiSend } from '../api';

export interface AuthUser {
  id: number;
  username: string;
  email: string | null;
  email_verified: boolean;
  role: string;
  timezone: string;
}

type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  isAdmin: boolean;
  refresh: () => Promise<void>;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  status: 'loading',
  user: null,
  isAdmin: false,
  refresh: async () => {},
  signIn: async () => {},
  signOut: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<AuthUser | null>(null);

  const refresh = useCallback(async () => {
    try {
      // Silent: a 401 here means "not signed in yet", not "session expired".
      const payload = await apiGet('/api/auth/me', { silent: true });
      setUser(payload as unknown as AuthUser);
      setStatus('authenticated');
    } catch {
      setUser(null);
      setStatus('anonymous');
    }
  }, []);

  useEffect(() => {
    // Deferred like AppShell does: avoids a synchronous setState inside the
    // effect, which the react-hooks lint rule rejects.
    const kickoff = window.setTimeout(() => {
      void refresh();
    }, 0);
    return () => window.clearTimeout(kickoff);
  }, [refresh]);

  const signIn = useCallback(async (username: string, password: string) => {
    await apiSend('/api/auth/login', 'POST', { username, password }, { silent: true });
    const payload = await apiGet('/api/auth/me', { silent: true });
    setUser(payload as unknown as AuthUser);
    setStatus('authenticated');
  }, []);

  const signOut = useCallback(async () => {
    try {
      await apiSend('/api/auth/logout', 'POST', {}, { silent: true });
    } catch {
      /* the local state is cleared regardless */
    }
    setUser(null);
    setStatus('anonymous');
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, isAdmin: user?.role === 'admin', refresh, signIn, signOut }),
    [status, user, refresh, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// The provider and its hook must stay in one module: they close over the same
// context object, and splitting them would only move the import around.
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth() {
  return useContext(AuthContext);
}
