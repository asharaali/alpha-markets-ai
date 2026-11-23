import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { isDemo, UNAUTHORIZED_EVENT } from "./lib/api";

interface Session {
  user: string | null; loading: boolean;
  login(username: string, password: string): Promise<string | null>;  // resolves to an error message or null
  logout(): Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const demo = isDemo();
  const [user, setUser] = useState<string | null>(demo ? "demo" : null);
  const [loading, setLoading] = useState(!demo);

  useEffect(() => {
    if (demo) return;
    fetch("/api/me", { credentials: "include" })
      .then((r) => r.json()).then((d) => setUser(d.user ?? null))
      .catch(() => setUser(null)).finally(() => setLoading(false));
    const drop = () => setUser(null);
    window.addEventListener(UNAUTHORIZED_EVENT, drop);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, drop);
  }, [demo]);

  const login = useCallback(async (username: string, password: string) => {
    const res = await fetch("/api/login", {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return body.message ?? "Sign-in failed.";
    setUser(body.user);
    return null;
  }, []);

  const logout = useCallback(async () => {
    await fetch("/api/logout", { method: "POST", credentials: "include" }).catch(() => undefined);
    setUser(null);
  }, []);

  return <SessionContext.Provider value={{ user, loading, login, logout }}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const s = useContext(SessionContext);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}
