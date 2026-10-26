import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { api, setCsrfToken, write } from './api';
interface Session { setup_required?: boolean; username?: string; authenticated?: boolean; csrf_token?: string; }
interface Auth { setupRequired: boolean; user: string | null; loading: boolean; refresh: () => Promise<void>; login: (username: string, password: string) => Promise<void>; logout: () => Promise<void>; }
const Context = createContext<Auth | null>(null);
export function AuthProvider({ children }: { children: ReactNode }) {
  const [setupRequired, setSetupRequired] = useState(false);
  const [user, setUser] = useState<string | null>(null); const [loading, setLoading] = useState(true);
  const accept = (session: Session) => { setSetupRequired(!!session.setup_required); setUser(session.authenticated ? session.username || 'Administrador' : null); setCsrfToken(session.csrf_token || ''); };
  const refresh = async () => { try { accept(await api<Session>('/auth/session')); } catch { accept({}); } finally { setLoading(false); } };
  useEffect(() => {
    let active = true;
    void api<Session>('/auth/session').then(session => {
      if (active) accept(session);
    }).catch(() => { if (active) accept({}); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  const login = async (username: string, password: string) => { accept(await write<Session>('/auth/login', { username, password })); await refresh(); };
  const logout = async () => { await write('/auth/logout', {}); accept({}); };
  return <Context.Provider value={{ setupRequired, user, loading, refresh, login, logout }}>{children}</Context.Provider>;
}
export function useAuth() { const value = useContext(Context); if (!value) throw new Error('Sessão indisponível'); return value; }
