import { useCallback, useEffect, useRef, useState } from "react";
import { api, AUTH_EXPIRED_EVENT, setCsrfToken } from "../lib/api";
import type { AuthState } from "../lib/types";
import { errorMessage } from "../lib/async";

export function useAuth() {
  const [state, setState] = useState<AuthState>({ user: null, csrf_token: null });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const logoutGuard = useRef(false);
  const clear = useCallback(() => {
    generation.current++;
    setCsrfToken(null);
    setState({ user: null, csrf_token: null });
    setLoading(false);
  }, []);
  const reload = useCallback(async () => {
    const version = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const next = await api.me();
      if (version !== generation.current) return;
      setCsrfToken(next.csrf_token ?? null);
      setState(next);
    } catch (error) {
      if (version === generation.current) setError(errorMessage(error));
    } finally {
      if (version === generation.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
    const expired = () => { clear(); setError("Your session expired. Sign in again to continue."); };
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    return () => {
      generation.current++;
      setCsrfToken(null);
      window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
    };
  }, [clear, reload]);
  async function logout() {
    if (logoutGuard.current) return;
    logoutGuard.current = true;
    setBusy(true);
    setError("");
    try {
      await api.logout();
      clear();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      logoutGuard.current = false;
      setBusy(false);
    }
  }
  return { ...state, loading, busy, error, reload, logout };
}
