import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { Agent, Feedback, MatchSession, PassReason, Recommendation, Saved, Skill } from "../lib/types";
import { useSavedSkills, type SavedSkillsState } from "./useSavedSkills";
import { errorMessage, type LoadState } from "../lib/async";

type MatchRequest = {
  task: string;
  agent: Agent;
  key: string;
  sessionId?: string;
  baseline: MatchSession | null;
};
type MatchSessionOptions = {
  enabled: boolean;
  canStart: () => boolean;
  onStarted: () => void;
  onReviewed: (action: "choice" | "undo") => void;
  onNotice: (message: string) => void;
};
const isMatching = (session: MatchSession | null) =>
  session?.status === "queued" || session?.status === "running";

export type MatchSessionController = {
  session: MatchSession | null;
  loadState: LoadState;
  loadError: string;
  reload: () => Promise<void>;
  saved: SavedSkillsState;
  matching: boolean;
  starting: boolean;
  cancelling: boolean;
  canMatch: () => boolean;
  canRetryMatch: boolean;
  pollError: string;
  matchError: string | undefined;
  retryMatch: () => void;
  start: (task: string, agent: Agent) => void;
  more: () => void;
  choose: (skill: Skill, action: "save" | "pass", reason?: PassReason | null) => void;
  undo: () => void;
  cancel: () => Promise<void>;
  canEdit: () => boolean;
  isStarting: () => boolean;
  remaining: Recommendation[];
  reviewedIds: Set<string>;
  lastChoice: Feedback | undefined;
};

export function useMatchSession({
  enabled, canStart, onStarted, onReviewed, onNotice,
}: MatchSessionOptions): MatchSessionController {
  const [session, setSession] = useState<MatchSession | null>(null);
  const currentSession = useRef<MatchSession | null>(null);
  // Match/cancel attempts and successful reviews invalidate older initial reads and polls.
  const version = useRef(0);
  const loadVersion = useRef(0);
  const [loadState, setLoadState] = useState<LoadState>(enabled ? "loading" : "idle");
  const [loadError, setLoadError] = useState("");
  const [starting, setStarting] = useState(false);
  const startGuard = useRef(false);
  const [cancelling, setCancelling] = useState(false);
  const cancelGuard = useRef(false);
  const [matchError, setMatchError] = useState<{ message: string; request: MatchRequest } | null>(null);
  const [pollError, setPollError] = useState("");
  const saved = useSavedSkills({ enabled, canWrite: () => !startGuard.current, onNotice });
  const matching = isMatching(session);
  const canMatch = () => enabled && canStart() && loadState === "ready";

  const adoptSession = useCallback((next: MatchSession | null) => {
    // Commands and retries need the latest session before React commits its next render.
    currentSession.current = next;
    setSession(next);
  }, []);
  const reload = useCallback(async () => {
    if (!enabled) return;
    const requestVersion = ++loadVersion.current;
    const generation = version.current;
    const current = () => requestVersion === loadVersion.current && generation === version.current;
    setLoadState("loading");
    try {
      const next = await api.current();
      if (!current()) return;
      adoptSession(next);
      setLoadState("ready");
      setLoadError("");
    } catch (error) {
      if (!current()) return;
      setLoadState("error");
      setLoadError(errorMessage(error));
    }
  }, [adoptSession, enabled]);
  useEffect(() => {
    void reload();
    return () => { version.current++; loadVersion.current++; };
  }, [reload]);

  useEffect(() => {
    if (!matching || !session) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const id = session.id;
    // Schedule after settlement so a slow check cannot overlap another poll.
    const poll = async () => {
      const generation = version.current;
      try {
        const next = await api.session(id);
        if (active && generation === version.current && currentSession.current?.id === id) {
          adoptSession(next);
          setPollError("");
        }
      } catch (error) {
        if (active && generation === version.current)
          setPollError(`Could not check matching: ${errorMessage(error)}`);
      }
      // An invalidated response can leave this session running, so keep polling while active.
      if (active) timer = setTimeout(poll, 1200);
    };
    timer = setTimeout(poll, 800);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [matching, session?.id, adoptSession]);

  async function reconcile(request: MatchRequest) {
    const next = request.sessionId ? await api.session(request.sessionId) : await api.current();
    if (!next || next.task !== request.task || next.agent !== request.agent)
      return null;
    // Continuations keep their ID; a new task needs a different session with the requested brief.
    const changed = request.sessionId
      ? next.id === request.sessionId && (next.latest_job.id !== request.baseline?.latest_job.id || isMatching(next))
      : next.id !== request.baseline?.id;
    return changed ? next : null;
  }

  async function startMatch(request: MatchRequest) {
    // Refs enforce mutual exclusion before React commits the busy indicators.
    if (startGuard.current || saved.isWriting() || isMatching(currentSession.current) || !canMatch()) return;
    startGuard.current = true;
    setStarting(true);
    setMatchError(null);
    const generation = ++version.current;
    try {
      let next: MatchSession | null;
      // Retrying the same key retrieves the accepted job even if reconciliation is unavailable.
      try {
        next = await api.match(request.task, request.agent, request.key, request.sessionId);
      } catch (error) {
        if (error instanceof ApiError) throw error;
        // A lost response may hide an accepted job; reconcile before offering a retry.
        next = await reconcile(request);
        if (!next) throw error;
      }
      if (generation !== version.current) return;
      adoptSession(next);
      setPollError("");
      onStarted();
    } catch (error) {
      if (generation === version.current) setMatchError({ message: errorMessage(error), request });
    } finally {
      startGuard.current = false;
      setStarting(false);
    }
  }

  function start(task: string, agent: Agent) {
    void startMatch({ task, agent, key: crypto.randomUUID(), baseline: currentSession.current });
  }
  function more() {
    const current = currentSession.current;
    if (current) void startMatch({
      task: current.task, agent: current.agent, key: crypto.randomUUID(), sessionId: current.id, baseline: current,
    });
  }

  function choose(skill: Skill, action: "save" | "pass", reason: PassReason | null = null) {
    const current = currentSession.current;
    if (!current || isMatching(current) || startGuard.current) return;
    void saved.mutate(`Retry ${action}`, async () => {
      // A stored retry retains its original task, which may have resumed matching since failure.
      if (isMatching(currentSession.current) && currentSession.current?.id === current.id)
        throw new Error("Wait for matching to finish before reviewing this task.");
      const result = await api.feedback(current.id, skill.id, action, reason);
      version.current++;
      // Saved ownership is global; only update the deck if this is still the same session.
      saved.setItems(result.saved.items);
      if (currentSession.current?.id === current.id) adoptSession({
        ...currentSession.current,
        feedback: [
          ...currentSession.current.feedback.filter((item) => item.id !== result.feedback.id),
          result.feedback,
        ],
      });
      onNotice(action === "save" ? `${skill.name} saved.` : `${skill.name} passed.`);
      onReviewed("choice");
    });
  }

  const reviewedIds = new Set(session?.feedback.map((item) => item.skill_id));
  const remaining = session?.recommendations.filter((item) => !reviewedIds.has(item.skill_id)) ?? [];
  // Feedback spans batches; undo must select a choice from the currently displayed batch.
  const lastChoice = session
    ? [...session.feedback].reverse().find((item) =>
        session.recommendations.some((recommendation) => recommendation.skill_id === item.skill_id))
    : undefined;

  function undo() {
    const current = currentSession.current;
    if (!lastChoice || !current || isMatching(current)) return;
    const choice = lastChoice;
    void saved.mutate("Retry undo", async () => {
      if (isMatching(currentSession.current) && currentSession.current?.id === current.id)
        throw new Error("Wait for matching to finish before undoing a choice.");
      const generation = version.current;
      let result: Saved;
      try {
        result = await api.undo(choice.id);
      } catch (error) {
        if (generation !== version.current) return;
        if (error instanceof ApiError && error.status !== 404 && error.status < 500) throw error;
        // A lost response or a retry's 404 can follow a committed deletion.
        const next = await api.session(current.id);
        if (generation !== version.current) return;
        if (next.id !== current.id || next.feedback.some((item) => item.id === choice.id)) throw error;
        // Observe deletion before reading the collection that it may have changed.
        result = await api.saved();
      }
      if (generation !== version.current) return;
      version.current++;
      // The server preserves bookmarks owned outside this deck when undo removes a review save.
      saved.setItems(result.items);
      if (currentSession.current?.id === current.id) adoptSession({
        ...currentSession.current,
        feedback: currentSession.current.feedback.filter((item) => item.id !== choice.id),
      });
      onNotice("Last choice undone.");
      onReviewed("undo");
    });
  }

  async function cancel(target?: { jobId: string; sessionId: string }) {
    const current = currentSession.current;
    if (!current || cancelGuard.current || startGuard.current) return;
    const requested = target ?? { jobId: current.latest_job.id, sessionId: current.id };
    cancelGuard.current = true;
    setCancelling(true);
    const generation = ++version.current;
    try {
      const next = await api.cancel(requested.jobId);
      if (generation !== version.current) return;
      // A poll started during cancellation must not restore the session's running state.
      version.current++;
      if (currentSession.current?.id === requested.sessionId) adoptSession(next);
      setPollError("");
      saved.setError((previous) => previous?.label === "Retry cancellation" ? null : previous);
    } catch (error) {
      if (generation !== version.current) return;
      saved.setError({
        message: `Could not stop matching: ${errorMessage(error)}`,
        label: "Retry cancellation",
        retry: () => void cancel(requested),
      });
    } finally {
      cancelGuard.current = false;
      setCancelling(false);
    }
  }

  return {
    session, loadState, loadError, reload, saved,
    matching, starting, cancelling, canMatch, pollError,
    matchError: matchError?.message,
    canRetryMatch: !!matchError && canMatch(),
    retryMatch: () => { if (matchError) void startMatch(matchError.request); },
    start, more, choose, undo, cancel,
    canEdit: () => !!currentSession.current && !startGuard.current &&
      !isMatching(currentSession.current) && !saved.isWriting(),
    isStarting: () => startGuard.current,
    remaining, reviewedIds, lastChoice,
  };
}
