import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Skill } from "../lib/types";

export function useSkillCache() {
  const [skillsById, setSkillsById] = useState<Record<string, Skill>>({});
  const cache = useRef<Record<string, Skill>>({});
  const add = useCallback((items: Skill[]) => {
    const next = { ...cache.current };
    for (const item of items) next[item.id] = item;
    cache.current = next;
    setSkillsById(next);
  }, []);
  const get = useCallback((id: string) => cache.current[id], []);
  return { skillsById, add, get };
}

export type SkillCache = ReturnType<typeof useSkillCache>;

export function useRecommendedSkills(ids: string[], { get, add }: SkillCache) {
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const key = JSON.stringify(ids);
  useEffect(() => {
    let active = true;
    const missing = (JSON.parse(key) as string[]).filter((id) => !get(id));
    setError("");
    if (!missing.length) return;
    void (async () => {
      // Match batches contain at most twelve skills; keep detail requests bounded.
      for (let offset = 0; offset < missing.length; offset += 12) {
        const results = await Promise.allSettled(missing.slice(offset, offset + 12).map((id) => api.skill(id)));
        if (!active) return;
        add(results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []));
        if (results.some((result) => result.status === "rejected"))
          setError("Some recommended skills could not be loaded. Try again to review them.");
      }
    })();
    return () => { active = false; };
  }, [key, attempt, get, add]);
  return { error, retry: () => setAttempt((value) => value + 1) };
}
