import type {
  Agent,
  Catalog,
  FeedbackResult,
  Health,
  MatchSession,
  PassReason,
  Profile,
  Saved,
  Skill,
} from "./types";

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
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
  return response.json() as Promise<T>;
}
const body = (value: unknown) => JSON.stringify(value);
export const api = {
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
  match: (task: string, agent: Agent, session_id?: string) =>
    request<MatchSession>("/matches", {
      method: "POST",
      body: body({ task, agent, ...(session_id ? { session_id } : {}) }),
    }),
  session: (id: string) =>
    request<MatchSession>(`/matches/${encodeURIComponent(id)}`),
  cancel: (id: string) =>
    request<MatchSession>(`/matches/${encodeURIComponent(id)}/cancel`, {
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
