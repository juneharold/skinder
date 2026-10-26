import { useCallback, useEffect, useRef, useState } from "react";
import { Diamond, SlidersHorizontal } from "lucide-react";
import { api } from "./lib/api";
import { defaultProfile, type Agent, type Health, type Profile, type Skill } from "./lib/types";
import { errorMessage as message, type LoadState } from "./lib/async";
import Preferences from "./components/Preferences";
import SkillDetail from "./components/SkillDetail";
import Dialog from "./components/Dialog";
import LoadNotice from "./components/LoadNotice";
import DiscoverView from "./views/DiscoverView";
import LibraryView from "./views/LibraryView";
import { useMatchSession } from "./hooks/useMatchSession";
import { useCatalog } from "./hooks/useCatalog";
import { useRecommendedSkills, useSkillCache } from "./hooks/useSkillCache";

type View = "discover" | "explore" | "saved";
type Resource = "profile" | "health";

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
  const [profile, setProfile] = useState<Profile>(defaultProfile);
  const [health, setHealth] = useState<Health | null>(null);
  const loadVersions = useRef<Record<Resource, number>>({ profile: 0, health: 0 });
  const [loads, setLoads] = useState<Record<Resource, LoadState>>({ profile: "loading", health: "loading" });
  const [loadErrors, setLoadErrors] = useState<Partial<Record<Resource, string>>>({});
  // Task edits stay separate from the active session until matching accepts the new brief.
  const [task, setTask] = useState("");
  const [agent, setAgent] = useState<Agent>("codex");
  const [editOpen, setEditOpen] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const preferenceGeneration = useRef(0);
  const profileGuard = useRef(false);
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState<{ message: string; draft: Profile } | null>(null);
  const [detail, setDetail] = useState<Skill | null>(null);
  const [toast, setToast] = useState("");
  const [showPassReasons, setShowPassReasons] = useState(false);
  const deckRef = useRef<HTMLDivElement>(null);
  const focusAfterMatching = useRef(false);
  const connected = !!health?.codex.installed && !!health?.codex.authenticated;
  const matches = useMatchSession({
    // Codex runs matching even when the requested skills target Claude Code.
    canStart: loads.profile === "ready" && loads.health === "ready" && !profileBusy && connected,
    onStarted: () => {
      focusAfterMatching.current = true;
      setEditOpen(false);
      navigate({ view: "discover" });
    },
    onReviewed: (action) => {
      if (action === "choice") setShowPassReasons(false);
      deckRef.current?.focus({ preventScroll: true });
    },
    onNotice: setToast,
  });
  const { session, matching, starting: matchBusy, canMatch, saved } = matches;
  const writeBusy = saved.busy;
  const actionError = saved.error;
  const cache = useSkillCache();
  const catalog = useCatalog({ q, category, cacheSkills: cache.add });
  const recommendedSkills = useRecommendedSkills(
    session?.recommendations.map((item) => item.skill_id) ?? [], cache,
  );
  useEffect(() => {
    cache.add(saved.items.map((item) => item.skill));
  }, [saved.items, cache.add]);
  const savedIds = new Set(saved.items.map((item) => item.skill.id));
  // A filtered page's total cannot establish whether the full catalog is exhausted.
  const catalogSize = health?.catalog_count ?? (
    !q && category === "All skills" && catalog.loadState === "ready" ? catalog.total : undefined
  );
  const load = useCallback(
    async (resource: Resource) => {
      const requestVersion = ++loadVersions.current[resource];
      setLoads((previous) => ({ ...previous, [resource]: "loading" }));
      const current = () =>
        requestVersion === loadVersions.current[resource];
      try {
        if (resource === "profile") {
          const next = await api.profile();
          if (!current()) return;
          setProfile(next);
          setAgent(next.agent);
        }
        if (resource === "health") {
          const next = await api.health();
          if (current()) setHealth(next);
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
    [],
  );
  useEffect(() => {
    (
      ["profile", "health"] as Resource[]
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
    if (!focusAfterMatching.current || matching || matchBusy) return;
    // Give keyboard review focus once, without taking it from an open dialog or another view.
    focusAfterMatching.current = false;
    if (view === "discover" && !detail && !preferencesOpen && !editOpen)
      deckRef.current?.focus({ preventScroll: true });
  }, [matching, matchBusy, view, detail, preferencesOpen, editOpen]);

  function url(next: typeof location) {
    // Preserve unrelated query parameters when updating the view and catalog filters.
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
  function submitTask(event: React.FormEvent) {
    event.preventDefault();
    if (task.trim().length >= 3) matches.start(task.trim(), agent);
  }
  function editTask() {
    if (!session || !matches.canEdit()) return;
    setTask(session.task);
    setAgent(session.agent);
    setEditOpen(true);
  }
  function openPreferences() {
    preferenceGeneration.current++;
    setPreferencesOpen(true);
  }
  async function savePreferences(draft: Profile) {
    if (profileGuard.current) return;
    profileGuard.current = true;
    setProfileBusy(true);
    setProfileError(null);
    // Only close the submitting dialog; a later dialog owns its own draft.
    const generation = preferenceGeneration.current;
    try {
      const next = await api.saveProfile(draft);
      // An initial profile read must not replace preferences that were just saved.
      loadVersions.current.profile++;
      setProfile(next);
      if (generation === preferenceGeneration.current)
        setPreferencesOpen(false);
      setToast("Preferences saved for future batches.");
    } catch (error) {
      // Retrying after the dialog closes must still submit the original draft.
      setProfileError({ message: message(error), draft });
      throw error;
    } finally {
      profileGuard.current = false;
      setProfileBusy(false);
    }
  }
  const resourceNotice = (resource: Resource) => (
    <LoadNotice label={resource === "health" ? "the Codex connection" : "preferences"} retryLabel={resource === "health" ? "connection" : "preferences"} state={loads[resource]} error={loadErrors[resource] ?? ""} onRetry={() => void load(resource)} />
  );
  const matchNotice = matches.matchError && (
    <div className="error-notice" role="alert">
      <p>Matching could not finish. {matches.matchError}</p>
      <button
        disabled={matchBusy}
        onClick={matches.retryMatch}
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
        rows={4}
        minLength={3}
        maxLength={4000}
        required
        value={task}
        disabled={matchBusy}
        onChange={(event) => setTask(event.target.value)}
        placeholder="For example, review the accessibility of my app."
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
          skinder<Diamond className="brand-mark" size={14} fill="currentColor" strokeWidth={0} aria-hidden="true" />
        </a>
        <nav aria-label="Main navigation">
          {(["discover", "explore", "saved"] as View[]).map((item) => (
            <a
              key={item}
              href={url({ ...location, view: item })}
              aria-current={view === item ? "page" : undefined}
              onClick={(event) => {
                if (event.ctrlKey || event.metaKey) return;
                event.preventDefault();
                navigate({ view: item });
              }}
            >
              {item[0].toUpperCase() + item.slice(1)}
              {item === "saved" && (
                <span className="nav-count">
                  {saved.loadState === "ready" ? saved.items.length : "—"}
                </span>
              )}
            </a>
          ))}
        </nav>
        <span className="connection" role="status">
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
          onClick={openPreferences}
        >
          <SlidersHorizontal size={20} aria-hidden="true" />
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
        {health?.retrieval_mode === "keyword-only" && (
          <p className="status-notice" role="status">
            Search is using keywords only. Matches may be less precise.
          </p>
        )}
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
                onClick={saved.dismissError}
              >
                Dismiss
              </button>
            </div>
          )}
        {view === "discover" ? (
          <DiscoverView
            matches={matches}
            skillsById={cache.skillsById}
            catalogSize={catalogSize}
            skillLoadError={recommendedSkills.error}
            onRetrySkills={recommendedSkills.retry}
            taskForm={taskForm}
            matchNotice={matchNotice}
            profileNotice={resourceNotice("profile")}
            preferencesDisabled={loads.profile !== "ready" || profileBusy}
            onPreferences={openPreferences}
            onEditTask={editTask}
            onOpenSkill={setDetail}
            onNavigate={(view) => navigate({ view })}
            exploreHref={url({ ...location, view: "explore" })}
            deckRef={deckRef}
            shortcutsDisabled={!!detail || preferencesOpen || editOpen}
            editOpen={editOpen}
            showPassReasons={showPassReasons}
            onTogglePassReasons={() => setShowPassReasons(!showPassReasons)}
          />
        ) : (
          <LibraryView
            view={view}
            q={q}
            category={category}
            catalog={catalog}
            saved={saved}
            skillsById={cache.skillsById}
            matchBusy={matchBusy}
            onFilters={navigate}
            onExplore={() => navigate({ view: "explore" })}
            onOpenSkill={setDetail}
          />
        )}
      </main>
      <footer className="site-footer">
        <span>Skinder</span>
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
            if (!matches.isStarting()) setEditOpen(false);
          }}
        >
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
          saveDisabled={writeBusy || matchBusy || saved.loadState !== "ready"}
          onSave={() => saved.bookmark(detail)}
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
