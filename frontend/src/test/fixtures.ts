import { vi } from "vitest";
import { api } from "../lib/api";
import {
  defaultProfile,
  type Agent,
  type AuthState,
  type Health,
  type Feedback,
  type MatchSession,
  type Profile,
  type Saved,
  type SavedItem,
  type Skill,
} from "../lib/types";

export function skill(
  id: string,
  name: string,
  category: Skill["category"] = "Design",
): Skill {
  return {
    id,
    name,
    slug: id,
    description: `A practical approach to ${name.toLowerCase()}.`,
    category,
    author: "Example",
    repository: "example/skills",
    source_url: `https://github.com/example/skills/blob/abc/${id}/SKILL.md`,
    provenance: "bundled",
    revision: "abc",
    license: "MIT",
    compatibility: ["codex", "claude-code"],
    requirements: [],
    tags: [category],
    workflow: ["Plan a focused result."],
    output_style: ["Clear, concise output."],
    evidence: [{ trait: "Concise", excerpt: "Keep it concise." }],
    install_commands: {
      codex: `npx skills add example/skills --skill ${id} --agent codex`,
    },
  };
}

export function sessionFor(
  skills: Skill[],
  overrides: Partial<MatchSession> = {},
): MatchSession {
  return {
    id: "session-1",
    task: "Build a thoughtful landing page",
    agent: "codex",
    latest_job: { id: "job-1", status: overrides.status ?? "completed", model: "hosted-model", error: null, error_code: null },
    status: "completed",
    recommendations: skills.map((item) => ({
      skill_id: item.id,
      task_fit: "Useful for this landing page.",
      taste_fit: "A concise working approach.",
      caveat: null,
    })),
    feedback: [],
    error: null,
    created_at: "2026-10-27T00:00:00Z",
    updated_at: "2026-10-27T00:00:00Z",
    ...overrides,
  };
}

export function savedItem(item: Skill, savedAt = "2026-10-27T01:00:00Z"): SavedItem {
  return { skill: item, saved_at: savedAt };
}

export const readyHealth: Health = {
  status: "ok", catalog_count: 3, indexed_count: 3, retrieval_mode: "hybrid",
  model: "hosted-model", matching_available: true,
};
export const signedIn: AuthState = {
  user: { id: "user-1", login: "alice", avatar_url: null }, csrf_token: "test-csrf-token",
};

type BackendOptions = {
  session?: MatchSession | null;
  saved?: SavedItem[];
  profile?: Profile;
  skills?: Skill[];
  health?: Partial<Health>;
  auth?: AuthState;
};

// This models one session for interaction tests; backend tests cover ownership and concurrent writes.
export function installBackend(options: BackendOptions = {}) {
  const skills = options.skills ?? [
    skill("design", "Thoughtful Design"),
    skill("writing", "Clear Writing", "Writing"),
    skill("research", "Careful Research", "Research"),
  ];
  let session = options.session === undefined ? sessionFor(skills) : options.session;
  let saved = structuredClone(options.saved ?? []);
  const directSaved = new Set(saved.map((item) => item.skill.id));
  const feedbackSaved = new Set<string>();
  let profile = structuredClone(options.profile ?? defaultProfile);
  const matchRequests: Array<{ task: string; agent: Agent; session_id?: string }> = [];
  const accepted = new Map<string, MatchSession>();
  let auth = options.auth ?? signedIn;

  // Real HTTP responses cannot mutate server state through shared object references.
  const savedResponse = (): Saved => ({ items: structuredClone(saved) });
  const removeIfUnowned = (id: string) => {
    if (!directSaved.has(id) && !feedbackSaved.has(id)) {
      saved = saved.filter((item) => item.skill.id !== id);
    }
  };
  const persistFeedback = (
    sessionId: string,
    skillId: string,
    action: "save" | "pass",
    reason: Feedback["reason"] = null,
  ) => {
    const existing = session?.id === sessionId
      ? session.feedback.find(
          (item) =>
            item.session_id === sessionId &&
            item.skill_id === skillId &&
            item.action === action,
        )
      : undefined;
    if (existing) return { feedback: existing, saved: savedResponse() };

    const feedback: Feedback = {
      id: `feedback-${sessionId}-${skillId}-${action}`,
      session_id: sessionId,
      skill_id: skillId,
      action,
      reason,
      created_at: "2026-10-27T01:00:00Z",
    };
    if (session?.id === sessionId) {
      session = {
        ...session,
        feedback: [
          ...session.feedback.filter((item) => item.id !== feedback.id),
          feedback,
        ],
      };
    }
    if (action === "save") {
      const item = skills.find((candidate) => candidate.id === skillId);
      feedbackSaved.add(skillId);
      if (item && !saved.some((entry) => entry.skill.id === skillId)) {
        saved = [...saved, savedItem(item)];
      }
    }
    return { feedback, saved: savedResponse() };
  };

  vi.spyOn(api, "me").mockImplementation(async () => structuredClone(auth));
  vi.spyOn(api, "logout").mockImplementation(async () => {
    auth = { user: null, csrf_token: null };
  });
  vi.spyOn(api, "catalog").mockImplementation(async ({ q = "", category, offset = 0, limit = 50 } = {}) => {
    const filtered = skills.filter((item) =>
      (!category || category === item.category) &&
      `${item.name} ${item.description} ${item.author} ${item.tags.join(" ")}`.toLowerCase().includes(q.toLowerCase()),
    );
    return {
      skills: structuredClone(filtered.slice(offset, offset + limit)),
      categories: [...new Set(skills.map((item) => item.category))],
      total: filtered.length,
      has_more: offset + limit < filtered.length,
    };
  });
  vi.spyOn(api, "skill").mockImplementation(async (id) => {
    const item = skills.find((candidate) => candidate.id === id);
    if (!item) throw new Error("This skill could not be found.");
    return structuredClone(item);
  });
  vi.spyOn(api, "profile").mockImplementation(async () => structuredClone(profile));
  vi.spyOn(api, "saveProfile").mockImplementation(async (next) => {
    profile = structuredClone(next);
    return structuredClone(profile);
  });
  vi.spyOn(api, "current").mockImplementation(async () => structuredClone(session));
  vi.spyOn(api, "session").mockImplementation(async () => {
    if (!session) throw new Error("There is no active matching session.");
    return structuredClone(session);
  });
  vi.spyOn(api, "saved").mockImplementation(async () => savedResponse());
  vi.spyOn(api, "health").mockResolvedValue({
    status: "ok",
    catalog_count: skills.length,
    indexed_count: skills.length,
    retrieval_mode: "hybrid",
    model: "hosted-model", matching_available: true,
    ...options.health,
  });
  vi.spyOn(api, "feedback").mockImplementation(async (...args) =>
    persistFeedback(...args),
  );
  vi.spyOn(api, "undo").mockImplementation(async (id) => {
    if (session) {
      const feedback = session.feedback.find((item) => item.id === id);
      if (feedback?.action === "save") {
        feedbackSaved.delete(feedback.skill_id);
        removeIfUnowned(feedback.skill_id);
      }
      session = {
        ...session,
        feedback: session.feedback.filter((item) => item.id !== id),
      };
    }
    return savedResponse();
  });
  vi.spyOn(api, "save").mockImplementation(async (id) => {
    const item = skills.find((candidate) => candidate.id === id);
    directSaved.add(id);
    if (item && !saved.some((entry) => entry.skill.id === id)) saved = [...saved, savedItem(item)];
    return savedResponse();
  });
  vi.spyOn(api, "unsave").mockImplementation(async (id) => {
    directSaved.delete(id);
    feedbackSaved.delete(id);
    saved = saved.filter((item) => item.skill.id !== id);
    return savedResponse();
  });
  vi.spyOn(api, "match").mockImplementation(async (task, agent, key, session_id) => {
    if (accepted.has(key)) return structuredClone(accepted.get(key)!);
    matchRequests.push({ task, agent, ...(session_id ? { session_id } : {}) });
    const previous = session_id && session?.id === session_id ? session : null;
    const previouslyRecommended = new Set(
      previous?.recommendations.map((item) => item.skill_id) ?? [],
    );
    const batch = previous
      ? skills.filter((item) => !previouslyRecommended.has(item.id))
      : skills.slice(0, 2);
    const next = sessionFor(batch, {
      id: session_id ?? `session-${matchRequests.length + 1}`,
      task,
      agent,
      latest_job: { id: `job-${matchRequests.length + 1}`, status: "completed", model: "hosted-model" },
      status: "completed",
      feedback: previous?.feedback ?? [],
    });
    session = next;
    accepted.set(key, next);
    return structuredClone(next);
  });
  vi.spyOn(api, "cancel").mockImplementation(async (id) => {
    if (session?.latest_job.id === id) session = { ...session, status: "cancelled", latest_job: { ...session.latest_job, status: "cancelled" } };
    if (!session) throw new Error("There is no active matching session.");
    return structuredClone(session);
  });

  return {
    skills,
    getSession: () => session,
    getSaved: () => structuredClone(saved),
    getProfile: () => structuredClone(profile),
    getMatchRequests: () => structuredClone(matchRequests),
  };
}

// Tests choose response order explicitly to reproduce races without relying on network delays.
export function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}
