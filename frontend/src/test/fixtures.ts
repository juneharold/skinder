import { vi } from "vitest";
import { api } from "../lib/api";
import {
  defaultProfile,
  type Feedback,
  type MatchSession,
  type SavedItem,
  type Skill,
} from "../lib/types";

export function skill(id: string, name: string): Skill {
  return {
    id,
    name,
    slug: id,
    description: `A practical approach to ${name.toLowerCase()}.`,
    category: "Design",
    author: "Example",
    repository: "example/skills",
    source_url: `https://github.com/example/skills/blob/abc/${id}/SKILL.md`,
    revision: "abc",
    license: "MIT",
    compatibility: ["codex", "claude-code"],
    requirements: [],
    tags: ["Design"],
    workflow: ["Plan a focused result."],
    output_style: ["Clear, concise output."],
    evidence: [{ trait: "Concise", excerpt: "Keep it concise." }],
    install_commands: {
      codex: `npx skills add example/skills --skill ${id} --agent codex`,
    },
  };
}

export function installBackend() {
  const skills = [
    skill("design", "Thoughtful Design"),
    skill("writing", "Clear Writing"),
  ];
  let session: MatchSession = {
    id: "session-1",
    task: "Build a thoughtful landing page",
    agent: "codex",
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
  };
  let saved: SavedItem[] = [];
  vi.spyOn(api, "catalog").mockResolvedValue({
    skills,
    categories: ["Design"],
  });
  vi.spyOn(api, "profile").mockResolvedValue({ ...defaultProfile });
  vi.spyOn(api, "saveProfile").mockImplementation(async (profile) => profile);
  vi.spyOn(api, "current").mockImplementation(async () =>
    structuredClone(session),
  );
  vi.spyOn(api, "session").mockImplementation(async () =>
    structuredClone(session),
  );
  vi.spyOn(api, "saved").mockImplementation(async () => ({
    items: structuredClone(saved),
  }));
  vi.spyOn(api, "health").mockResolvedValue({
    status: "ok",
    catalog_count: 2,
    codex: {
      installed: false,
      authenticated: false,
      message: "Install Codex.",
    },
  });
  vi.spyOn(api, "feedback").mockImplementation(
    async (session_id, skill_id, action, reason = null) => {
      const value: Feedback = {
        id: `feedback-${skill_id}`,
        session_id,
        skill_id,
        action,
        reason,
        created_at: "2026-10-27T01:00:00Z",
      };
      session = { ...session, feedback: [...session.feedback, value] };
      if (action === "save")
        saved.push({
          skill: skills.find((item) => item.id === skill_id)!,
          saved_at: value.created_at,
        });
      return value;
    },
  );
  vi.spyOn(api, "undo").mockImplementation(async (id) => {
    const feedback = session.feedback.find((item) => item.id === id);
    saved = saved.filter((item) => item.skill.id !== feedback?.skill_id);
    session = {
      ...session,
      feedback: session.feedback.filter((item) => item.id !== id),
    };
    return { ok: true };
  });
  return { skills, getSession: () => session, getSaved: () => saved };
}
