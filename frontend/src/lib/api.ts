import type {
  Agent,
  AuthState,
  Catalog,
  FeedbackResult,
  Health,
  MatchSession,
  PassReason,
  Profile,
  Saved,
  Skill,
} from "./types";

// Keep HTTP failures distinct from lost responses so matching can reconcile uncertain submissions.
export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export const AUTH_EXPIRED_EVENT = "skinder:auth-expired";
let csrfToken: string | null = null;
export function setCsrfToken(value: string | null) { csrfToken = value; }

// Retry policy belongs to each operation; a write may succeed before its response is lost.
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const writing = options.method && !["GET", "HEAD"].includes(options.method);
  if (writing && !csrfToken) throw new ApiError("Sign in to continue.", 401);
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      ...(writing ? { "X-CSRF-Token": csrfToken! } : {}),
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (response.status === 401) {
    csrfToken = null;
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : `The request couldn't be completed (${response.status}). Please try again.`,
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
const body = (value: unknown) => JSON.stringify(value);
export const api = {
  me: () => request<AuthState>("/auth/me"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  health: () => request<Health>("/health"),
  catalog: (
    filters: { q?: string; category?: string; offset?: number; limit?: number } = {},
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    return request<Catalog>(`/catalog?${params}`, { signal });
  },
  skill: (id: string) => request<Skill>(`/skills/${encodeURIComponent(id)}`),
  profile: () => request<Profile>("/profile"),
  saveProfile: (profile: Profile) =>
    request<Profile>("/profile", { method: "PUT", body: body(profile) }),
  current: () => request<MatchSession | null>("/matches/current"),
  match: (task: string, agent: Agent, idempotencyKey: string, session_id?: string) =>
    request<MatchSession>("/matches", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: body({ task, agent, ...(session_id ? { session_id } : {}) }),
    }),
  session: (id: string) =>
    request<MatchSession>(`/matches/${encodeURIComponent(id)}`),
  cancel: (id: string) =>
    request<MatchSession>(`/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }),
  feedback: (
    session_id: string,
    skill_id: string,
    action: "save" | "pass",
    reason: PassReason | null = null,
  ) =>
    request<FeedbackResult>("/feedback", {
      method: "POST",
      body: body({ session_id, skill_id, action, reason }),
    }),
  undo: (id: string) =>
    request<Saved>(`/feedback/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  saved: () => request<Saved>("/saved"),
  save: (id: string) =>
    request<Saved>(`/saved/${encodeURIComponent(id)}`, {
      method: "PUT",
    }),
  unsave: (id: string) =>
    request<Saved>(`/saved/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};
