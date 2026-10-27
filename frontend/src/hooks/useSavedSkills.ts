import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { SavedItem, Skill } from "../lib/types";
import { errorMessage, type LoadState } from "../lib/async";

export type ActionError = {
  message: string;
  label: string;
  retry: () => void;
  // Route a bookmark failure to its original skill even if a different detail dialog opens.
  skillId?: string;
  kind?: "bookmark";
};

type SavedSkillsOptions = {
  enabled: boolean;
  canWrite: () => boolean;
  onNotice: (message: string) => void;
};

export type SavedSkillsState = {
  items: SavedItem[];
  loadState: LoadState;
  loadError: string;
  busy: boolean;
  error: ActionError | null;
  reload: () => Promise<void>;
  bookmark: (skill: Skill) => void;
  dismissError: () => void;
};

export function useSavedSkills({ enabled, canWrite, onNotice }: SavedSkillsOptions) {
  const [items, setItems] = useState<SavedItem[]>([]);
  const [loadState, setLoadState] = useState<LoadState>(enabled ? "loading" : "idle");
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ActionError | null>(null);
  const writing = useRef(false);
  const version = useRef(0);
  const loadVersion = useRef(0);

  const reload = useCallback(async () => {
    if (!enabled) return;
    const requestVersion = ++loadVersion.current;
    const generation = version.current;
    // Both newer reloads and intervening writes can make this response obsolete.
    const current = () => requestVersion === loadVersion.current && generation === version.current;
    setLoadState("loading");
    try {
      const next = await api.saved();
      if (!current()) return;
      setItems(next.items);
      setLoadState("ready");
      setLoadError("");
    } catch (error) {
      if (!current()) return;
      setLoadState("error");
      setLoadError(errorMessage(error));
    }
  }, [enabled]);
  useEffect(() => {
    void reload();
    return () => { version.current++; loadVersion.current++; };
  }, [reload]);

  async function mutate(label: string, operation: () => Promise<void>, skill?: Skill) {
    // Bookmarks, reviews, and undo share full collection responses, so serialize their writes.
    if (!enabled || writing.current || !canWrite()) return;
    writing.current = true;
    // Reads started before or during a mutation cannot replace its saved collection.
    version.current++;
    setBusy(true);
    setError(null);
    try {
      await operation();
      setLoadState("ready");
    } catch (error) {
      setError({
        message: `${skill ? `${skill.name}: ` : ""}${errorMessage(error)}`,
        label,
        skillId: skill?.id,
        kind: skill ? "bookmark" : undefined,
        retry: () => void mutate(label, operation, skill),
      });
    } finally {
      // Also reject reads begun during the write, including when its response was lost.
      version.current++;
      writing.current = false;
      setBusy(false);
    }
  }

  function bookmark(skill: Skill) {
    // Capture the intended operation so a lost response can be retried idempotently.
    const remove = items.some((item) => item.skill.id === skill.id);
    void mutate(remove ? "Retry removal" : "Retry save", async () => {
      setItems((await (remove ? api.unsave(skill.id) : api.save(skill.id))).items);
      onNotice(remove ? `${skill.name} removed from saved.` : `${skill.name} saved.`);
    }, skill);
  }

  return {
    items, loadState, loadError, busy, error, reload, bookmark,
    dismissError: () => setError(null),
    mutate, setItems, setError, isWriting: () => writing.current,
  };
}
