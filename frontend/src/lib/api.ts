import type {
  Agent,
  Feedback,
  Health,
  MatchSession,
  PassReason,
  Profile,
  SavedItem,
  Skill,
} from "./types";

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
    throw new Error(
      typeof detail === "string"
        ? detail
        : `The request couldn't be completed (${response.status}). Please try again.`,
    );
  }
  return response.json() as Promise<T>;
}
const body = (value: unknown) => JSON.stringify(value);
export const api = {
  health: () => request<Health>("/health"),
  catalog: () => request<{ skills: Skill[]; categories: string[] }>("/catalog"),
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
    request<Feedback>("/feedback", {
      method: "POST",
      body: body({ session_id, skill_id, action, reason }),
    }),
  undo: (id: string) =>
    request<{ ok: boolean }>(`/feedback/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  saved: () => request<{ items: SavedItem[] }>("/saved"),
  unsave: (id: string) =>
    request<{ ok: boolean }>(`/saved/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};
