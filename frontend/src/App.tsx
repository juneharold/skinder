import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Bookmark,
  Check,
  Pencil,
  RotateCcw,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { api, ApiError } from "./lib/api";
import {
  defaultProfile,
  type Agent,
  type Health,
  type MatchSession,
  type Profile,
  type PassReason,
  type SavedItem,
  type Skill,
} from "./lib/types";
import Preferences from "./components/Preferences";
import SkillDetail from "./components/SkillDetail";
import SkillCard from "./components/SkillCard";
import Dialog from "./components/Dialog";

type View = "discover" | "explore" | "saved";
type Resource = "catalog" | "profile" | "session" | "saved" | "health";
type LoadState = "loading" | "ready" | "error";
type MatchRequest = {
  task: string;
  agent: Agent;
  sessionId?: string;
  baseline: MatchSession | null;
  uncertain?: boolean;
};
type ActionError = {
  message: string;
  label: string;
  retry: () => void;
  skillId?: string;
  kind?: "bookmark";
};
const message = (error: unknown) =>
  error instanceof Error ? error.message : "Please try again.";
const isMatching = (session: MatchSession | null) =>
  session?.status === "pending" || session?.status === "running";
function readLocation() {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  return {
    view: (view === "explore" || view === "saved" ? view : "discover") as View,
    q: params.get("q") ?? "",
    category: params.get("category") ?? "All skills",
  };
}

export default function App() {
  const [location, setLocation] = useState(readLocation);
  const { view, q, category } = location;
  const [skills, setSkills] = useState<Skill[]>([]);
  const [profile, setProfile] = useState<Profile>(defaultProfile);
  const [health, setHealth] = useState<Health | null>(null);
  const [session, setSession] = useState<MatchSession | null>(null);
  const sessionRef = useRef<MatchSession | null>(null);
  const sessionVersion = useRef(0);
  const [saved, setSaved] = useState<SavedItem[]>([]);
  const savedVersion = useRef(0);
  const loadVersions = useRef<Record<Resource, number>>({
    catalog: 0,
    profile: 0,
    session: 0,
    saved: 0,
    health: 0,
  });
  const [loads, setLoads] = useState<Record<Resource, LoadState>>({
    catalog: "loading",
    profile: "loading",
    session: "loading",
    saved: "loading",
    health: "loading",
  });
  const [loadErrors, setLoadErrors] = useState<
    Partial<Record<Resource, string>>
  >({});
  const [task, setTask] = useState("");
  const [agent, setAgent] = useState<Agent>("codex");
  const [editOpen, setEditOpen] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const preferenceGeneration = useRef(0);
  const profileGuard = useRef(false);
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState<{
    message: string;
    draft: Profile;
  } | null>(null);
  const [detail, setDetail] = useState<Skill | null>(null);
  const [writeBusy, setWriteBusy] = useState(false);
  const writeGuard = useRef(false);
  const [matchBusy, setMatchBusy] = useState(false);
  const matchGuard = useRef(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const cancelGuard = useRef(false);
  const [matchError, setMatchError] = useState<{
    message: string;
    request: MatchRequest;
  } | null>(null);
  const [actionError, setActionError] = useState<ActionError | null>(null);
  const [pollError, setPollError] = useState("");
  const [toast, setToast] = useState("");
  const [showPassReasons, setShowPassReasons] = useState(false);
  const deckRef = useRef<HTMLDivElement>(null);
  const focusAfterMatching = useRef(false);
  const matching = isMatching(session);
  const connected = !!health?.codex.installed && !!health?.codex.authenticated;
  const adoptSession = useCallback((next: MatchSession | null) => {
    sessionRef.current = next;
    setSession(next);
  }, []);

  const load = useCallback(
    async (resource: Resource) => {
      const requestVersion = ++loadVersions.current[resource];
      setLoads((previous) => ({ ...previous, [resource]: "loading" }));
      const savedGeneration = savedVersion.current;
      const generation = sessionVersion.current;
      const current = () =>
        requestVersion === loadVersions.current[resource] &&
        (resource !== "session" || generation === sessionVersion.current) &&
        (resource !== "saved" || savedGeneration === savedVersion.current);
      try {
        if (resource === "catalog") {
          const next = await api.catalog();
          if (current()) setSkills(next.skills);
        }
        if (resource === "profile") {
          const next = await api.profile();
          if (current()) {
            setProfile(next);
            setAgent(next.agent);
          }
        }
        if (resource === "health") {
          const next = await api.health();
          if (current()) setHealth(next);
        }
        if (resource === "session") {
          const next = await api.current();
          if (current()) adoptSession(next);
        }
        if (resource === "saved") {
          const next = await api.saved();
          if (current()) setSaved(next.items);
        }
        if (!current()) return;
        setLoads((previous) => ({ ...previous, [resource]: "ready" }));
        setLoadErrors((previous) => ({ ...previous, [resource]: undefined }));
      } catch (error) {
        if (!current()) return;
        setLoads((previous) => ({ ...previous, [resource]: "error" }));
        setLoadErrors((previous) => ({
          ...previous,
          [resource]: message(error),
        }));
      }
    },
    [adoptSession],
  );
  useEffect(() => {
    (
      ["catalog", "profile", "session", "saved", "health"] as Resource[]
    ).forEach((resource) => void load(resource));
  }, [load]);
  useEffect(() => {
    const pop = () => setLocation(readLocation());
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(""), 5000);
    return () => clearTimeout(timeout);
  }, [toast]);
  useEffect(() => {
    if (!matching || !session) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const id = session.id;
    const poll = async () => {
      const generation = sessionVersion.current;
      try {
        const next = await api.session(id);
        if (
          active &&
          generation === sessionVersion.current &&
          sessionRef.current?.id === id
        ) {
          adoptSession(next);
          setPollError("");
        }
      } catch (error) {
        if (active && generation === sessionVersion.current)
          setPollError(`Could not check matching: ${message(error)}`);
      }
      if (active) timer = setTimeout(poll, 1200);
    };
    timer = setTimeout(poll, 800);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [matching, session?.id, adoptSession]);
  useEffect(() => {
    if (!focusAfterMatching.current || matching || matchBusy) return;
    focusAfterMatching.current = false;
    if (view === "discover" && !detail && !preferencesOpen && !editOpen)
      deckRef.current?.focus({ preventScroll: true });
  }, [matching, matchBusy, view, detail, preferencesOpen, editOpen]);

  function url(next: typeof location) {
    const params = new URLSearchParams(window.location.search);
    params.set("view", next.view);
    if (next.q) params.set("q", next.q);
    else params.delete("q");
    if (next.category !== "All skills") params.set("category", next.category);
    else params.delete("category");
    return `${window.location.pathname}?${params.toString()}`;
  }
  function navigate(next: Partial<typeof location>, replace = false) {
    const value = { ...location, ...next };
    window.history[replace ? "replaceState" : "pushState"]({}, "", url(value));
    setLocation(value);
  }
  const canMatch =
    loads.profile === "ready" &&
    loads.session === "ready" &&
    loads.health === "ready" &&
    !profileBusy &&
    connected;
  async function reconcile(request: MatchRequest) {
    const next = request.sessionId
      ? await api.session(request.sessionId)
      : await api.current();
    const changed = request.sessionId
      ? next &&
        (next.updated_at !== request.baseline?.updated_at || isMatching(next))
      : next &&
        next.id !== request.baseline?.id &&
        next.task === request.task &&
        next.agent === request.agent;
    return changed ? next : null;
  }
  async function startMatch(request: MatchRequest) {
    if (
      matchGuard.current ||
      writeGuard.current ||
      isMatching(sessionRef.current) ||
      !canMatch
    )
      return;
    matchGuard.current = true;
    setMatchBusy(true);
    setMatchError(null);
    const generation = ++sessionVersion.current;
    try {
      let next: MatchSession | null = null;
      if (request.uncertain) next = await reconcile(request);
      if (!next) {
        request = { ...request, uncertain: false };
        try {
          next = await api.match(
            request.task,
            request.agent,
            request.sessionId,
          );
        } catch (error) {
          if (error instanceof ApiError) throw error;
          request = { ...request, uncertain: true };
          next = await reconcile(request);
          if (!next) throw error;
        }
      }
      if (generation === sessionVersion.current) {
        focusAfterMatching.current = true;
        adoptSession(next);
        setPollError("");
        setEditOpen(false);
        navigate({ view: "discover" });
      }
    } catch (error) {
      if (generation === sessionVersion.current)
        setMatchError({ message: message(error), request });
    } finally {
      matchGuard.current = false;
      setMatchBusy(false);
    }
  }
  function submitTask(event: React.FormEvent) {
    event.preventDefault();
    if (task.trim().length >= 3)
      void startMatch({
        task: task.trim(),
        agent,
        baseline: sessionRef.current,
      });
  }
  function more() {
    if (session)
      void startMatch({
        task: session.task,
        agent: session.agent,
        sessionId: session.id,
        baseline: session,
      });
  }
  function editTask() {
    if (!session || matchGuard.current || matching || writeGuard.current) return;
    setTask(session.task);
    setAgent(session.agent);
    setEditOpen(true);
  }
  async function mutate(
    label: string,
    operation: () => Promise<void>,
    skill?: Skill,
  ) {
    if (writeGuard.current || matchGuard.current) return;
    writeGuard.current = true;
    savedVersion.current++;
    setWriteBusy(true);
    setActionError(null);
    try {
      await operation();
      setLoads((previous) => ({ ...previous, saved: "ready" }));
    } catch (error) {
      setActionError({
        message: `${skill ? `${skill.name}: ` : ""}${message(error)}`,
        label,
        skillId: skill?.id,
        kind: skill ? "bookmark" : undefined,
        retry: () => void mutate(label, operation, skill),
      });
    } finally {
      savedVersion.current++;
      writeGuard.current = false;
      setWriteBusy(false);
    }
  }
  function choose(action: "save" | "pass", reason: PassReason | null = null) {
    const current = sessionRef.current;
    if (!currentSkill || !current || matching || matchGuard.current) return;
    const skill = currentSkill;
    void mutate(`Retry ${action}`, async () => {
      if (
        isMatching(sessionRef.current) &&
        sessionRef.current?.id === current.id
      )
        throw new Error(
          "Wait for matching to finish before reviewing this task.",
        );
      const result = await api.feedback(current.id, skill.id, action, reason);
      setShowPassReasons(false);
      sessionVersion.current++;
      setSaved(result.saved.items);
      if (sessionRef.current?.id === current.id)
        adoptSession({
          ...sessionRef.current,
          feedback: [
            ...sessionRef.current.feedback.filter(
              (item) => item.id !== result.feedback.id,
            ),
            result.feedback,
          ],
        });
      setToast(
        action === "save" ? `${skill.name} saved.` : `${skill.name} passed.`,
      );
      deckRef.current?.focus({ preventScroll: true });
    });
  }
  function undo() {
    const current = sessionRef.current;
    if (!lastChoice || !current || matching) return;
    const choice = lastChoice;
    void mutate("Retry undo", async () => {
      if (
        isMatching(sessionRef.current) &&
        sessionRef.current?.id === current.id
      )
        throw new Error("Wait for matching to finish before undoing a choice.");
      const result = await api.undo(choice.id);
      sessionVersion.current++;
      setSaved(result.items);
      if (sessionRef.current?.id === current.id)
        adoptSession({
          ...sessionRef.current,
          feedback: sessionRef.current.feedback.filter(
            (item) => item.id !== choice.id,
          ),
        });
      setToast("Last choice undone.");
      deckRef.current?.focus({ preventScroll: true });
    });
  }
  function bookmark(skill: Skill) {
    const remove = saved.some((item) => item.skill.id === skill.id);
    void mutate(
      remove ? "Retry removal" : "Retry save",
      async () => {
        setSaved(
          (await (remove ? api.unsave(skill.id) : api.save(skill.id))).items,
        );
        setToast(
          remove ? `${skill.name} removed from saved.` : `${skill.name} saved.`,
        );
      },
      skill,
    );
  }
  async function savePreferences(draft: Profile) {
    if (profileGuard.current) return;
    profileGuard.current = true;
    setProfileBusy(true);
    setProfileError(null);
    const generation = preferenceGeneration.current;
    try {
      const next = await api.saveProfile(draft);
      loadVersions.current.profile++;
      setProfile(next);
      if (generation === preferenceGeneration.current)
        setPreferencesOpen(false);
      setToast("Preferences saved for future batches.");
    } catch (error) {
      setProfileError({ message: message(error), draft });
      throw error;
    } finally {
      profileGuard.current = false;
      setProfileBusy(false);
    }
  }
  async function cancel() {
    if (!session || cancelGuard.current || matchGuard.current) return;
    cancelGuard.current = true;
    setCancelBusy(true);
    const id = session.id;
    const generation = ++sessionVersion.current;
    try {
      const next = await api.cancel(id);
      if (generation === sessionVersion.current) {
        sessionVersion.current++;
        adoptSession(next);
        setPollError("");
        setActionError((previous) =>
          previous?.label === "Retry cancellation" ? null : previous,
        );
      }
    } catch (error) {
      setActionError({
        message: `Could not stop matching: ${message(error)}`,
        label: "Retry cancellation",
        retry: () => void cancel(),
      });
    } finally {
      cancelGuard.current = false;
      setCancelBusy(false);
    }
  }
  const reviewedIds = new Set(session?.feedback.map((item) => item.skill_id));
  const remaining =
    session?.recommendations.filter(
      (item) => !reviewedIds.has(item.skill_id),
    ) ?? [];
  const recommendation = remaining[0];
  const currentSkill = skills.find(
    (skill) => skill.id === recommendation?.skill_id,
  );
  const lastChoice = session
    ? [...session.feedback]
        .reverse()
        .find((item) =>
          session.recommendations.some(
            (recommendation) => recommendation.skill_id === item.skill_id,
          ),
        )
    : undefined;
  const total = session?.recommendations.length ?? 0;
  const reviewed = total - remaining.length;
  const hasMoreCandidates = skills.some(
    (skill) =>
      session &&
      skill.compatibility.includes(session.agent) &&
      !reviewedIds.has(skill.id),
  );
  const retryMatching =
    session?.status === "failed" || session?.status === "cancelled";
  const canContinue = total > 0 && hasMoreCandidates;
  const savedIds = new Set(saved.map((item) => item.skill.id));
  const categories = [...new Set(skills.map((skill) => skill.category))].sort();
  const visibleSkills = (
    view === "saved" ? saved.map((item) => item.skill) : skills
  ).filter(
    (skill) =>
      (category === "All skills" || skill.category === category) &&
      `${skill.name} ${skill.description} ${skill.author} ${skill.tags.join(" ")}`
        .toLowerCase()
        .includes(q.toLowerCase()),
  );
  const resourceNotice = (resource: Resource) =>
    loads[resource] === "error" ? (
      <div className="error-notice" role="alert">
        <p>
          Could not load{" "}
          {resource === "session" ? "your current task" : resource}.{" "}
          {loadErrors[resource]}
        </p>
        <button onClick={() => void load(resource)}>Retry {resource}</button>
      </div>
    ) : null;
  const matchNotice = matchError && (
    <div className="error-notice" role="alert">
      <p>Matching could not finish. {matchError.message}</p>
      <button
        disabled={matchBusy}
        onClick={() => void startMatch(matchError.request)}
      >
        Retry matching
      </button>
    </div>
  );
  const taskForm = (
    <form onSubmit={submitTask} className="task-form">
      <label className="field-label" htmlFor="task">
        What are you working on?
      </label>
      <textarea
        id="task"
        rows={5}
        minLength={3}
        maxLength={4000}
        required
        value={task}
        disabled={matchBusy}
        onChange={(event) => setTask(event.target.value)}
        placeholder="Describe your task and the result you have in mind."
      />
      <label className="field-label" htmlFor="target-agent">
        Target agent
      </label>
      <select
        id="target-agent"
        value={agent}
        disabled={matchBusy || loads.profile !== "ready"}
        onChange={(event) => setAgent(event.target.value as Agent)}
      >
        <option value="codex">Codex</option>
        <option value="claude-code">Claude Code</option>
      </select>
      <button
        className="primary-button"
        disabled={
          !canMatch ||
          matchBusy ||
          matching ||
          writeBusy ||
          task.trim().length < 3
        }
      >
        {matchBusy ? "Starting matching…" : "Find skills"}
        <ArrowRight size={18} />
      </button>
    </form>
  );

  return (
    <div className="app-shell">
      <header className="site-header">
        <a
          className="brand"
          href={url({ ...location, view: "discover" })}
          onClick={(event) => {
            event.preventDefault();
            navigate({ view: "discover" });
          }}
          aria-label="Skinder home"
        >
          skinder<span className="brand-diamond">♦</span>
        </a>
        <nav aria-label="Main navigation">
          {(["discover", "explore", "saved"] as View[]).map((item) => (
            <a
              key={item}
              href={url({ ...location, view: item })}
              aria-current={view === item ? "page" : undefined}
              onClick={(event) => {
                if (!event.ctrlKey && !event.metaKey) {
                  event.preventDefault();
                  navigate({ view: item });
                }
              }}
            >
              {item[0].toUpperCase() + item.slice(1)}
              {item === "saved" && (
                <span className="nav-count">
                  {loads.saved === "ready" ? saved.length : "—"}
                </span>
              )}
            </a>
          ))}
        </nav>
        <span className="connection">
          <span
            className={
              connected ? "connection-dot connected" : "connection-dot"
            }
          />
          {loads.health === "loading"
            ? "Checking Codex"
            : connected
              ? "Codex connected"
              : "Codex unavailable"}
        </span>
        <button
          className="icon-button header-preferences"
          aria-label="Preferences"
          disabled={loads.profile !== "ready" || profileBusy}
          onClick={() => {
            preferenceGeneration.current++;
            setPreferencesOpen(true);
          }}
        >
          <SlidersHorizontal size={20} />
        </button>
      </header>
      <main>
        {loads.health === "ready" && !connected && (
          <div className="setup-notice">
            <div>
              <strong>Connect Codex to find matches.</strong>
              <p>
                Install the Codex CLI and sign in with <code>codex login</code>.
                You can still explore and save skills.
              </p>
            </div>
            <button onClick={() => void load("health")}>
              Recheck connection
            </button>
          </div>
        )}
        {resourceNotice("health")}
        {profileBusy && (
          <p className="status-notice" role="status">
            Saving preferences…
          </p>
        )}
        {profileError && !preferencesOpen && (
          <div className="error-notice" role="alert">
            <p>Could not save preferences. {profileError.message}</p>
            <button
              disabled={profileBusy}
              onClick={() =>
                void savePreferences(profileError.draft).catch(() => {})
              }
            >
              Retry preferences
            </button>
          </div>
        )}
        {actionError &&
          (!detail ||
            actionError.skillId !== detail.id ||
            actionError.kind !== "bookmark") && (
            <div className="error-notice global-error" role="alert">
              <p>{actionError.message}</p>
              <button disabled={writeBusy} onClick={actionError.retry}>
                {actionError.label}
              </button>
              <button
                className="text-button"
                onClick={() => setActionError(null)}
              >
                Dismiss
              </button>
            </div>
          )}
        {view === "discover" ? (
          <>
            {resourceNotice("profile")}
            {resourceNotice("session")}
            {loads.session === "loading" ? (
              <div className="empty-state" role="status">
                <span className="diamond">♦</span>
                <h1>Loading your table…</h1>
              </div>
            ) : loads.session === "error" ? (
              <div className="empty-state">
                <h1>Your current task is unavailable.</h1>
                <p>
                  Retry loading your task to pick up where you left off. You can
                  still browse the catalog.
                </p>
              </div>
            ) : !session ? (
              <section className="welcome-layout">
                <div className="welcome-intro">
                  <span className="eyebrow">
                    A good skill changes the game.
                  </span>
                  <h1>
                    Your task.
                    <br />A better hand.
                  </h1>
                  <p>
                    Find agent skills that fit what you’re doing and how you
                    like to work.
                  </p>
                  <div className="how-it-works">
                    <span>
                      <b>01</b> Describe your task.
                    </span>
                    <span>
                      <b>02</b> Review one skill at a time.
                    </span>
                    <span>
                      <b>03</b> Save a fit. Pass on the rest.
                    </span>
                  </div>
                  <a
                    href={url({ ...location, view: "explore" })}
                    onClick={(event) => {
                      event.preventDefault();
                      navigate({ view: "explore" });
                    }}
                  >
                    Or explore the catalog <ArrowUpRight size={16} />
                  </a>
                </div>
                <div className="task-entry">
                  <span className="card-index">
                    A<br />♦
                  </span>
                  <h2>Deal me in.</h2>
                  {taskForm}
                  {matchNotice}
                </div>
              </section>
            ) : (
              <div className="discover-layout">
                <aside className="task-sidebar">
                  <span className="eyebrow">Current task</span>
                  <h1>
                    {session.task.length > 120
                      ? `${session.task.slice(0, 120).trimEnd()}…`
                      : session.task}
                  </h1>
                  {session.task.length > 120 && (
                    <details className="full-task" key={session.id}>
                      <summary>Read full task</summary>
                      <p>{session.task}</p>
                    </details>
                  )}
                  <div className="sidebar-section">
                    <span className="eyebrow">Target</span>
                    <strong>
                      {session.agent === "codex" ? "Codex" : "Claude Code"}
                    </strong>
                  </div>
                  <div className="sidebar-actions">
                    <button
                      disabled={loads.profile !== "ready" || profileBusy}
                      onClick={() => {
                        preferenceGeneration.current++;
                        setPreferencesOpen(true);
                      }}
                    >
                      <SlidersHorizontal size={18} />
                      Preferences
                    </button>
                    <button
                      disabled={matchBusy || matching || writeBusy}
                      onClick={editTask}
                    >
                      <Pencil size={18} />
                      Edit task
                    </button>
                  </div>
                  <p className="small muted">
                    Save adds a skill to your collection. Pass leaves it out of
                    this deck.
                  </p>
                </aside>
                <section
                  className="deck-column"
                  aria-label="Skill deck"
                  ref={deckRef}
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (
                      event.repeat ||
                      event.ctrlKey ||
                      event.metaKey ||
                      event.altKey ||
                      (event.target as HTMLElement).closest(
                        "input,textarea,select,dialog,a",
                      ) ||
                      matching ||
                      matchBusy ||
                      writeBusy ||
                      detail ||
                      preferencesOpen ||
                      editOpen
                    )
                      return;
                    if (event.key === "ArrowLeft") {
                      event.preventDefault();
                      choose("pass");
                    }
                    if (event.key === "ArrowRight") {
                      event.preventDefault();
                      choose("save");
                    }
                    if (event.key.toLowerCase() === "z") {
                      event.preventDefault();
                      undo();
                    }
                  }}
                >
                  <span className="sr-only" role="status">
                    {!matching && session.status === "completed"
                      ? `Matching complete. ${remaining.length} skills left to review.`
                      : ""}
                  </span>
                  {!editOpen && matchNotice}
                  {pollError && (
                    <p className="error-notice" role="alert">
                      {pollError} We’ll keep checking.
                    </p>
                  )}
                  {matching || matchBusy ? (
                    <div className="empty-state matching-state" role="status">
                      <span className="diamond">♦</span>
                      <span className="eyebrow">Finding your next hand</span>
                      <h2>Looking for a good fit.</h2>
                      <p>
                        Matching skills to your task and preferences. This can
                        take a little while.
                      </p>
                      <button
                        disabled={matchBusy || cancelBusy}
                        onClick={() => void cancel()}
                      >
                        {cancelBusy ? "Stopping…" : "Stop matching"}
                      </button>
                    </div>
                  ) : (
                    <>
                      {session.status === "failed" && (
                        <div className="error-notice" role="alert">
                          <p>Matching failed. {session.error}</p>
                          {currentSkill && (
                            <button
                              disabled={!canMatch || writeBusy}
                              onClick={more}
                            >
                              Retry matching
                            </button>
                          )}
                        </div>
                      )}
                      {session.status === "cancelled" && (
                        <div className="status-notice" role="status">
                          Matching stopped.{" "}
                          {remaining.length > 0
                            ? "Your previous cards are still here."
                            : "You can try again when you’re ready."}
                        </div>
                      )}
                      {resourceNotice("catalog")}
                      {loads.catalog === "loading" ? (
                        <div className="empty-state" role="status">
                          Loading skills…
                        </div>
                      ) : currentSkill ? (
                        <div className="card-stack">
                          <SkillCard
                            skill={currentSkill}
                            recommendation={recommendation}
                            disabled={writeBusy || matchBusy}
                            onOpen={() => setDetail(currentSkill)}
                            onSwipe={choose}
                          />
                          <div className="deck-actions">
                            <button
                              aria-label="Undo last choice"
                              disabled={!lastChoice || writeBusy}
                              onClick={undo}
                            >
                              <RotateCcw size={18} />
                              <span>Undo</span>
                            </button>
                            <button
                              aria-label="Pass on this skill"
                              disabled={writeBusy}
                              onClick={() => choose("pass")}
                            >
                              <ArrowLeft size={18} />
                              <span>Pass</span>
                            </button>
                            <button
                              className="primary-button"
                              aria-label={
                                savedIds.has(currentSkill.id)
                                  ? "Keep saved"
                                  : "Save this skill"
                              }
                              disabled={writeBusy}
                              onClick={() => choose("save")}
                            >
                              <span>
                                {savedIds.has(currentSkill.id)
                                  ? "Keep saved"
                                  : "Save"}
                              </span>
                              <ArrowRight size={18} />
                            </button>
                          </div>
                          <div className="pass-reasons">
                            <button
                              className="text-button"
                              disabled={writeBusy}
                              aria-expanded={showPassReasons}
                              onClick={() =>
                                setShowPassReasons(!showPassReasons)
                              }
                            >
                              Pass with a reason
                            </button>
                            {showPassReasons && (
                              <div
                                className="reason-options"
                                aria-label="Reason for passing"
                              >
                                <button
                                  disabled={writeBusy}
                                  onClick={() => choose("pass", "wrong-task")}
                                >
                                  Not for this task
                                </button>
                                <button
                                  disabled={writeBusy}
                                  onClick={() => choose("pass", "wrong-style")}
                                >
                                  Not my style
                                </button>
                                <button
                                  disabled={writeBusy}
                                  onClick={() =>
                                    choose("pass", "too-much-setup")
                                  }
                                >
                                  Too much setup
                                </button>
                              </div>
                            )}
                          </div>
                        </div>
                      ) : (
                        loads.catalog === "ready" && (
                          <div className="empty-state" role="status">
                            <span className="diamond">♦</span>
                            <span className="eyebrow">
                              {total
                                ? "All cards considered"
                                : "The search is complete"}
                            </span>
                            <h2>
                              {session.status === "cancelled"
                                ? "Matching cancelled."
                                : session.status === "failed"
                                  ? "We couldn’t finish this hand."
                                  : total
                                    ? hasMoreCandidates
                                      ? "Batch reviewed."
                                      : "All available skills reviewed."
                                    : session.feedback.length > 0
                                      ? "No further matches."
                                      : "No matching skills yet."}
                            </h2>
                            <p>
                              {retryMatching
                                ? "Retry matching for this task, or change your brief. Your saved skills are still in your collection."
                                : canContinue
                                  ? "Your saved skills are ready whenever you need them. Find another batch for this task, or edit your brief."
                                  : "Try a more specific task, adjust your preferences, or browse the catalog."}
                            </p>
                            <button
                              className="primary-button"
                              disabled={
                                writeBusy ||
                                ((retryMatching || canContinue) && !canMatch)
                              }
                              onClick={retryMatching || canContinue ? more : editTask}
                            >
                              {retryMatching
                                ? "Retry matching"
                                : canContinue
                                  ? "Find more for this task"
                                  : "Change task"}
                              <ArrowRight size={18} />
                            </button>
                            <button
                              className="text-button"
                              onClick={() => navigate({ view: total ? "saved" : "explore" })}
                            >
                              {total
                                ? "View saved skills"
                                : "Explore the catalog"}
                              <ArrowUpRight size={16} />
                            </button>
                            {lastChoice && (
                              <button
                                className="text-button"
                                aria-label="Undo last choice"
                                disabled={writeBusy}
                                onClick={undo}
                              >
                                <RotateCcw size={16} />
                                Undo last choice
                              </button>
                            )}
                          </div>
                        )
                      )}
                    </>
                  )}
                </section>
                <aside className="progress-sidebar">
                  <div>
                    <span className="eyebrow">Deck progress</span>
                    <p className="progress-count">
                      {matching && !total ? (
                        <span>Preparing your deck</span>
                      ) : (
                        <>{reviewed} <span>of {total} reviewed</span></>
                      )}
                    </p>
                    <div className="progress-dots" aria-hidden="true">
                      {Array.from({ length: total }, (_, index) => (
                        <span
                          key={index}
                          className={index < reviewed ? "done" : ""}
                        />
                      ))}
                    </div>
                  </div>
                  <div className="shortcuts">
                    <span className="eyebrow">Keyboard shortcuts</span>
                    <p>
                      <kbd>←</kbd>Pass
                    </p>
                    <p>
                      <kbd>→</kbd>Save
                    </p>
                    <p>
                      <kbd>Z</kbd>Undo
                    </p>
                    <span className="small muted">
                      Available when the deck is focused. You can also swipe a
                      card.
                    </span>
                  </div>
                </aside>
              </div>
            )}
          </>
        ) : (
          <section className="library">
            <div className="library-heading">
              <div>
                <span className="eyebrow">
                  {view === "saved" ? "Your collection" : "The skill catalog"}
                </span>
                <h1>
                  {view === "saved"
                    ? "A hand worth keeping."
                    : "Find your next advantage."}
                </h1>
                <p>
                  {view === "saved"
                    ? "Skills you’ve saved, ready for your next project."
                    : "Browse the source. Save what fits. Make it yours."}
                </p>
              </div>
              <span className="library-diamond" aria-hidden="true">
                ♦
              </span>
            </div>
            <div className="library-filters">
              <label className="search-field">
                <Search size={18} />
                <span className="sr-only">Search skills</span>
                <input
                  value={q}
                  placeholder="Search skills, authors, or tags"
                  onChange={(event) =>
                    navigate({ q: event.target.value }, true)
                  }
                />
              </label>
              <label className="category-select">
                <span className="sr-only">Category</span>
                <select
                  value={category}
                  onChange={(event) =>
                    navigate({ category: event.target.value })
                  }
                >
                  <option>All skills</option>
                  {categories.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
            </div>
            {resourceNotice(view === "saved" ? "saved" : "catalog")}
            {view === "explore" && resourceNotice("saved")}
            {loads[view === "saved" ? "saved" : "catalog"] === "loading" ? (
              <div className="empty-state" role="status">
                Loading skills…
              </div>
            ) : (
              loads[view === "saved" ? "saved" : "catalog"] === "ready" && (
                <>
                  <p className="result-count" role="status">
                    {visibleSkills.length}{" "}
                    {visibleSkills.length === 1 ? "skill" : "skills"}
                    {category !== "All skills" ? ` in ${category}` : ""}
                    {q ? ` matching “${q}”` : ""}
                  </p>
                  <div className="skill-grid">
                    {visibleSkills.map((skill, index) => (
                      <article className="catalog-card" key={skill.id}>
                        <div className="catalog-top">
                          <span className="small-index">
                            {String(index + 1).padStart(2, "0")} ♦
                          </span>
                          <span className="eyebrow">{skill.category}</span>
                        </div>
                        <h2>
                          <button
                            className="title-button"
                            onClick={() => setDetail(skill)}
                          >
                            {skill.name}
                          </button>
                        </h2>
                        <span className="small muted">by {skill.author}</span>
                        <p>{skill.description}</p>
                        <div className="tag-row">
                          {skill.tags.slice(0, 3).map((tag) => (
                            <span key={tag}>{tag}</span>
                          ))}
                        </div>
                        <div className="catalog-actions">
                          <button
                            className="text-button"
                            onClick={() => setDetail(skill)}
                          >
                            View details
                            <ArrowUpRight size={16} />
                          </button>
                          <button
                            className={
                              savedIds.has(skill.id)
                                ? "bookmark-button is-saved"
                                : "bookmark-button"
                            }
                            aria-label={
                              savedIds.has(skill.id)
                                ? `Remove ${skill.name} from saved`
                                : `Save ${skill.name}`
                            }
                            disabled={
                              writeBusy || matchBusy || loads.saved !== "ready"
                            }
                            onClick={() => bookmark(skill)}
                          >
                            {savedIds.has(skill.id) ? (
                              <Check size={17} />
                            ) : (
                              <Bookmark size={17} />
                            )}
                            <span>
                              {savedIds.has(skill.id) ? "Saved" : "Save"}
                            </span>
                          </button>
                        </div>
                      </article>
                    ))}
                  </div>
                  {visibleSkills.length === 0 && (
                    <div className="empty-state">
                      <span className="diamond">♦</span>
                      <h2>
                        {q || category !== "All skills"
                          ? "No skills match these filters."
                          : "Your collection starts here."}
                      </h2>
                      <p>
                        {q || category !== "All skills"
                          ? "Try another search or category."
                          : "Explore the catalog or review a deck to save your first skill."}
                      </p>
                      {q || category !== "All skills" ? (
                        <button
                          onClick={() =>
                            navigate({ q: "", category: "All skills" })
                          }
                        >
                          Clear filters
                        </button>
                      ) : (
                        <button onClick={() => navigate({ view: "explore" })}>
                          Explore skills
                          <ArrowRight size={16} />
                        </button>
                      )}
                    </div>
                  )}
                </>
              )
            )}
          </section>
        )}
      </main>
      <footer className="site-footer">
        <span>
          SKINDER <span className="red">♦</span> Pick your skills. Make your
          move.
        </span>
        <span>Real skills. Original sources.</span>
      </footer>
      <div className="toast" role="status" aria-live="polite">
        {toast}
      </div>
      {editOpen && (
        <Dialog
          title="Edit task"
          closeDisabled={matchBusy}
          onClose={() => {
            if (!matchGuard.current) setEditOpen(false);
          }}
        >
          <h2>A new task. A new hand.</h2>
          <p className="muted">
            Starting a new task replaces this deck. Your saved skills stay in
            your collection.
          </p>
          {taskForm}
          {matchNotice}
          <button
            className="text-button"
            disabled={matchBusy}
            onClick={() => setEditOpen(false)}
          >
            Cancel
          </button>
        </Dialog>
      )}
      {preferencesOpen && (
        <Preferences
          profile={profile}
          onClose={() => {
            preferenceGeneration.current++;
            setPreferencesOpen(false);
          }}
          onSave={savePreferences}
        />
      )}
      {detail && (
        <SkillDetail
          skill={detail}
          agent={session?.agent ?? agent}
          recommendation={session?.recommendations.find(
            (item) => item.skill_id === detail.id,
          )}
          saved={savedIds.has(detail.id)}
          saveDisabled={writeBusy || matchBusy || loads.saved !== "ready"}
          onSave={() => bookmark(detail)}
          saveError={
            actionError?.kind === "bookmark" &&
            actionError.skillId === detail.id
              ? actionError
              : null
          }
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  );
}
