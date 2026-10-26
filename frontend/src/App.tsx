import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Bookmark,
  Check,
  ChevronDown,
  Compass,
  Heart,
  LoaderCircle,
  RotateCcw,
  Search,
  SlidersHorizontal,
  Sparkles,
  X,
  Zap,
} from "lucide-react";
import { api } from "./lib/api";
import {
  defaultProfile,
  type Agent,
  type Health,
  type MatchSession,
  type PassReason,
  type Profile,
  type SavedItem,
  type Skill,
} from "./lib/types";
import { BrandMark, SkillIcon } from "./components/Icons";
import Preferences from "./components/Preferences";
import SkillDetail from "./components/SkillDetail";
import SkillCard from "./components/SkillCard";

type View = "discover" | "explore" | "saved";
const examples = [
  {
    label: "Build something beautiful",
    task: "Build a distinctive landing page for my new product. I want thoughtful typography and a clean, playful visual style.",
    category: "Design",
  },
  {
    label: "Find the right words",
    task: "Turn my rough product notes into clear, warm website copy. Keep it concise and avoid marketing jargon.",
    category: "Writing",
  },
  {
    label: "Make sense of the data",
    task: "Explore a CSV of customer feedback, find useful patterns, and explain the results with clear charts.",
    category: "Data",
  },
];

export default function App() {
  const [view, setView] = useState<View>("discover");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [profile, setProfile] = useState<Profile>(defaultProfile);
  const [health, setHealth] = useState<Health | null>(null);
  const [session, setSession] = useState<MatchSession | null>(null);
  const [saved, setSaved] = useState<SavedItem[]>([]);
  const [task, setTask] = useState("");
  const [agent, setAgent] = useState<Agent>("codex");
  const [loading, setLoading] = useState(true);
  const [bootError, setBootError] = useState("");
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [detail, setDetail] = useState<Skill | null>(null);
  const [busy, setBusy] = useState(false);
  const [departing, setDeparting] = useState<"save" | "pass" | null>(null);
  const [showPassReasons, setShowPassReasons] = useState(false);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("All skills");
  const [previewIndex, setPreviewIndex] = useState(0);
  const taskRef = useRef<HTMLTextAreaElement>(null);
  const deckRef = useRef<HTMLDivElement>(null);
  const operationRef = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setBootError("");
    try {
      const [catalog, storedProfile, current, bookmarks] = await Promise.all([
        api.catalog(),
        api.profile(),
        api.current(),
        api.saved(),
      ]);
      setSkills(catalog.skills);
      setProfile(storedProfile);
      setAgent(current?.agent ?? storedProfile.agent);
      setSession(current);
      setTask(current?.task ?? "");
      setSaved(bookmarks.items);
    } catch (error) {
      setBootError(
        error instanceof Error
          ? error.message
          : "Could not connect to Skinder.",
      );
    } finally {
      setLoading(false);
    }
    api
      .health()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 4500);
    return () => clearTimeout(timer);
  }, [toast]);
  const matching =
    session?.status === "pending" || session?.status === "running";
  useEffect(() => {
    if (!matching || !session) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api.session(session.id);
        if (active) {
          setSession(next);
          setError("");
        }
      } catch (error) {
        if (active)
          setError(
            error instanceof Error
              ? error.message
              : "Connection interrupted. Retrying…",
          );
      }
      if (active) timer = setTimeout(poll, 1200);
    };
    timer = setTimeout(poll, 800);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [matching, session?.id]);

  const reviewedIds = new Set(
    session?.feedback.map((item) => item.skill_id) ?? [],
  );
  const remaining =
    session?.recommendations.filter(
      (item) => !reviewedIds.has(item.skill_id),
    ) ?? [];
  const currentRecommendation = remaining[0];
  const currentSkill = skills.find(
    (skill) => skill.id === currentRecommendation?.skill_id,
  );
  const previewSkills = skills.filter((skill) =>
    [
      "frontend-design",
      "brainstorming",
      "copywriting",
      "scientific-writing",
    ].includes(skill.slug),
  );
  const previews = previewSkills.length ? previewSkills : skills;
  const previewSkill = previews[previewIndex % Math.max(previews.length, 1)];
  const activeSkill = session ? currentSkill : previewSkill;
  const categories = [...new Set(skills.map((skill) => skill.category))].sort();
  const hasTaste =
    profile.planning !== "balanced" ||
    profile.communication !== "balanced" ||
    profile.verification !== "balanced" ||
    !!profile.output_style.trim();
  const lastChoice = session
    ? [...session.feedback]
        .reverse()
        .find((feedback) =>
          session.recommendations.some(
            (item) => item.skill_id === feedback.skill_id,
          ),
        )
    : undefined;

  async function startMatch(more = false) {
    if (operationRef.current || matching || task.trim().length < 3) return;
    operationRef.current = true;
    setBusy(true);
    setError("");
    setShowPassReasons(false);
    try {
      const next = await api.match(
        more && session ? session.task : task.trim(),
        more && session ? session.agent : agent,
        more ? session?.id : undefined,
      );
      setSession(next);
      setView("discover");
      if (window.matchMedia("(max-width: 650px)").matches) {
        requestAnimationFrame(() => deckRef.current?.scrollIntoView({
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
          block: "start",
        }));
      }
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Could not start matching.",
      );
    } finally {
      operationRef.current = false;
      setBusy(false);
    }
  }
  async function choose(
    action: "save" | "pass",
    reason: PassReason | null = null,
  ) {
    if (!currentSkill || !session || operationRef.current || matching) return;
    operationRef.current = true;
    setBusy(true);
    setError("");
    setShowPassReasons(false);
    try {
      const feedback = await api.feedback(
        session.id,
        currentSkill.id,
        action,
        reason,
      );
      setDeparting(action);
      await new Promise((resolve) => setTimeout(resolve, 190));
      setSession((previous) =>
        previous
          ? {
              ...previous,
              feedback: [
                ...previous.feedback.filter((item) => item.id !== feedback.id),
                feedback,
              ],
            }
          : previous,
      );
      setToast(
        action === "save"
          ? `${currentSkill.name} is in your saved skills.`
          : reason === "wrong-style"
            ? "Got it. Your next batch will take that into account."
            : "Passed. On to the next possibility.",
      );
      const bookmarks = await api.saved();
      setSaved(bookmarks.items);
    } catch (error) {
      setError(
        error instanceof Error
          ? error.message
          : "Your choice could not be saved.",
      );
    } finally {
      setDeparting(null);
      operationRef.current = false;
      setBusy(false);
    }
  }
  async function undo() {
    if (!lastChoice || !session || operationRef.current || matching) return;
    operationRef.current = true;
    setBusy(true);
    setError("");
    try {
      await api.undo(lastChoice.id);
      const [updated, bookmarks] = await Promise.all([
        api.session(session.id),
        api.saved(),
      ]);
      setSession(updated);
      setSaved(bookmarks.items);
      setToast("Last choice undone. Take another look.");
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Could not undo that choice.",
      );
    } finally {
      operationRef.current = false;
      setBusy(false);
    }
  }
  async function cancel() {
    if (!session || busy) return;
    setBusy(true);
    try {
      setSession(await api.cancel(session.id));
      setError("");
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Could not stop matching.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function removeSaved(skill: Skill) {
    try {
      await api.unsave(skill.id);
      setSaved((items) => items.filter((item) => item.skill.id !== skill.id));
      setToast(`${skill.name} removed from saved skills.`);
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Could not remove this skill.",
      );
    }
  }
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (
        view !== "discover" ||
        preferencesOpen ||
        detail ||
        !session ||
        matching ||
        busy ||
        event.repeat || event.metaKey || event.ctrlKey || event.altKey
      )
        return;
      if (
        (event.target as HTMLElement).closest(
          'input,textarea,select,[contenteditable="true"],dialog',
        )
      )
        return;
      if (event.key === "ArrowLeft" && currentSkill) {
        event.preventDefault();
        void choose("pass");
      }
      if (event.key === "ArrowRight" && currentSkill) {
        event.preventDefault();
        void choose("save");
      }
      if (
        event.key.toLowerCase() === "z" &&
        !event.ctrlKey &&
        !event.metaKey &&
        lastChoice
      ) {
        event.preventDefault();
        void undo();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  });

  const visibleSkills = (
    view === "saved" ? saved.map((item) => item.skill) : skills
  ).filter(
    (skill) =>
      (category === "All skills" || category === skill.category) &&
      `${skill.name} ${skill.description} ${skill.author} ${skill.tags.join(" ")}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const changeView = (next: View) => {
    setView(next);
    setSearch("");
    setCategory("All skills");
    setError("");
  };
  const displayError =
    error || (session?.status === "failed" ? session.error : "");

  return (
    <div className="app-shell">
      <header className="site-header">
        <a
          className="brand"
          href="#"
          aria-label="Skinder home"
          onClick={(event) => {
            event.preventDefault();
            changeView("discover");
          }}
        >
          <BrandMark />
          <span>
            skinder<span className="brand-dot">.</span>
          </span>
          <span className="beta-label">BETA</span>
        </a>
        <nav aria-label="Main navigation">
          <button
            className={view === "discover" ? "active" : ""}
            onClick={() => changeView("discover")}
          >
            <Sparkles size={16} />
            Discover
          </button>
          <button
            className={view === "explore" ? "active" : ""}
            onClick={() => changeView("explore")}
          >
            <Compass size={16} />
            Explore
          </button>
          <button
            className={view === "saved" ? "active" : ""}
            onClick={() => changeView("saved")}
          >
            <Heart size={16} />
            Saved<span className="nav-count">{saved.length}</span>
          </button>
        </nav>
        <button
          className="connection-status"
          onClick={() => {
            api
              .health()
              .then((value) => {
                setHealth(value);
                setToast(value.codex.message);
              })
              .catch(() => setToast("Could not reach the local backend."));
          }}
          title="Check Codex connection"
        >
          <span
            className={
              health?.codex.authenticated ? "status-dot online" : "status-dot"
            }
          />
          <span>
            {health?.codex.authenticated
              ? "Codex connected"
              : health
                ? "Codex needs setup"
                : "Local workspace"}
          </span>
        </button>
      </header>

      <main>
        {bootError ? (
          <div className="connection-error">
            <div className="empty-icon">
              <Zap />
            </div>
            <h1>Let’s reconnect.</h1>
            <p>{bootError}</p>
            <p className="muted">
              Make sure the local Skinder backend is running.
            </p>
            <button className="primary-button" onClick={() => void load()}>
              Try again
              <RotateCcw size={17} />
            </button>
          </div>
        ) : loading ? (
          <div className="loading-page">
            <LoaderCircle className="spin" size={30} />
            <p>Getting the good skills together…</p>
          </div>
        ) : view === "discover" ? (
          <>
            <section className="discovery-layout">
              <div className="task-column">
                <div className="eyebrow hero-eyebrow">
                  <span className="tiny-spark">✳</span> GOOD WORK STARTS WITH A
                  GOOD MATCH
                </div>
                <h1>
                  Find your
                  <br />
                  kind of{" "}
                  <span className="accent-word">
                    skill
                    <svg viewBox="0 0 180 15" aria-hidden="true">
                      <path d="M3 11C48 0 126 1 175 7" />
                    </svg>
                    .
                  </span>
                </h1>
                <p className="hero-description">
                  Your task. Your taste. A skill that gets both.
                  <br />
                  Less searching, more <span>“that’s the one.”</span>
                </p>
                <form
                  className="task-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void startMatch();
                  }}
                >
                  <label className="field-label" htmlFor="task">
                    What are you working on?
                    <span>Start with a little context</span>
                  </label>
                  <div className="task-input-wrap">
                    <textarea
                      id="task"
                      ref={taskRef}
                      value={task}
                      onChange={(event) => setTask(event.target.value)}
                      maxLength={4000}
                      rows={4}
                      placeholder="I’m building a landing page and want it to feel thoughtful, playful, and nothing like a template…"
                      disabled={matching}
                    />
                    <span className="textarea-decoration">
                      <Sparkles size={16} />
                    </span>
                  </div>
                  <div className="agent-line">
                    <span>Find skills for</span>
                    <div className="agent-segment" aria-label="Target agent">
                      <button
                        type="button"
                        aria-pressed={agent === "codex"}
                        onClick={() => setAgent("codex")}
                        disabled={matching}
                      >
                        <span className="agent-glyph">◉</span>Codex
                      </button>
                      <button
                        type="button"
                        aria-pressed={agent === "claude-code"}
                        onClick={() => setAgent("claude-code")}
                        disabled={matching}
                      >
                        <span className="claude-glyph">✳</span>Claude Code
                      </button>
                    </div>
                  </div>
                  <button
                    className="taste-button"
                    type="button"
                    onClick={() => setPreferencesOpen(true)}
                  >
                    <span>
                      <SlidersHorizontal size={17} />
                      <strong>
                        {hasTaste
                          ? "Your taste is in the mix"
                          : "Make it more you"}
                      </strong>
                      <span className="optional-label">
                        {hasTaste ? "Personalized" : "Optional"}
                      </span>
                    </span>
                    <ArrowUpRight size={17} />
                  </button>
                  <button
                    className="primary-button match-button"
                    type="submit"
                    disabled={
                      task.trim().length < 3 ||
                      busy ||
                      matching ||
                      !skills.length
                    }
                  >
                    {matching ? (
                      <>
                        <LoaderCircle size={18} className="spin" />
                        Finding your kind of skill…
                      </>
                    ) : (
                      <>
                        <Sparkles size={18} />
                        {session ? "Find new matches" : "Find my matches"}
                        <ArrowRight size={19} />
                      </>
                    )}
                  </button>
                  <p className="match-footnote">
                    <span className="status-dot online" />
                    Powered by your Codex login. Saved on this device.
                  </p>
                </form>
                {displayError && (
                  <div className="inline-error" role="alert">
                    <span>{displayError}</span>
                    <button
                      className="text-button"
                      onClick={() =>
                        void startMatch(
                          !!session &&
                            session.task === task.trim() &&
                            session.agent === agent,
                        )
                      }
                      disabled={busy || matching}
                    >
                      Retry
                      <RotateCcw size={14} />
                    </button>
                  </div>
                )}
                {health && !health.codex.authenticated && (
                  <div className="setup-note">
                    <strong>
                      {health.codex.installed
                        ? "Connect Codex to find your matches."
                        : "Install Codex to find your matches."}
                    </strong>
                    <p>
                      {health.codex.installed
                        ? "Run codex login in your terminal, then check the connection above. You can explore the catalog in the meantime."
                        : "Install the Codex CLI and sign in. You can still explore every skill and copy installation commands."}
                    </p>
                  </div>
                )}
                <div className="catalog-proof">
                  <div className="mini-avatars">
                    <span>A</span>
                    <span>V</span>
                    <span>✳</span>
                    <span>↗</span>
                  </div>
                  <p>
                    <strong>{skills.length} real skills.</strong> Many ways to
                    do great work.
                  </p>
                </div>
              </div>

              <div className="deck-column" ref={deckRef}>
                <div className="deck-caption">
                  <span>
                    <span className="status-dot coral" />
                    {matching
                      ? "A GOOD MATCH IS WORTH A MOMENT"
                      : session
                        ? "A LITTLE CHEMISTRY. A LOT OF CAPABILITY."
                        : "GREAT SKILLS. DIFFERENT PERSONALITIES."}
                  </span>
                  <span className="deck-caption-number">
                    {session
                      ? `${session.recommendations.length - remaining.length} / ${session.recommendations.length}`
                      : "YOUR NEXT COLLABORATOR"}
                  </span>
                </div>
                <div className={`card-stage ${matching ? "is-matching" : ""}`}>
                  <div className="card-back back-two" />
                  <div className="card-back back-one" />
                  {matching ? (
                    <div className="matching-card">
                      <div className="matching-orbit">
                        <BrandMark />
                        <span />
                        <span />
                        <span />
                      </div>
                      <span className="eyebrow">LOOKING FOR THE CLICK</span>
                      <h2>
                        Getting to know
                        <br />
                        your task.
                      </h2>
                      <p>
                        Looking through {skills.length} skills for an approach
                        that feels right for you.
                      </p>
                      <div className="matching-steps">
                        <span>
                          <Check size={14} />
                          Your task and taste
                        </span>
                        <span>
                          <LoaderCircle className="spin" size={14} />
                          Finding the right approaches
                        </span>
                      </div>
                      <button
                        className="text-button"
                        disabled={busy}
                        onClick={() => void cancel()}
                      >
                        Stop matching
                        <X size={14} />
                      </button>
                    </div>
                  ) : activeSkill ? (
                    <SkillCard
                      key={`${activeSkill.id}-${!!session}`}
                      skill={activeSkill}
                      recommendation={
                        session ? currentRecommendation : undefined
                      }
                      preview={!session}
                      disabled={busy}
                      departing={departing}
                      onOpen={() => setDetail(activeSkill)}
                      onSwipe={(action) => void choose(action)}
                    />
                  ) : (
                    <div className="finished-card">
                      <div className="finished-art">
                        <Heart size={46} strokeWidth={1.3} />
                        <Sparkles className="finished-spark" size={20} />
                      </div>
                      <span className="eyebrow">
                        {session?.status === "failed"
                          ? "LET’S TRY THAT AGAIN"
                          : session?.status === "cancelled"
                            ? "TAKE YOUR TIME"
                            : "THAT’S THIS ROUND"}
                      </span>
                      <h2>
                        {session?.status === "failed"
                          ? "A little hiccup."
                          : session?.status === "cancelled"
                            ? "Matching paused."
                            : session?.recommendations.length === 0
                              ? "Nothing quite fits. Yet."
                              : "Your next good match is out there."}
                      </h2>
                      <p>
                        {session?.status === "failed"
                          ? "We couldn’t finish this batch. Your saved skills are right where you left them."
                          : session?.status === "cancelled"
                            ? "Ready for another look? Start again whenever you like."
                            : session?.recommendations.length === 0
                              ? "Try a more specific task, adjust your taste, or explore what’s in the catalog."
                              : "Look for more possibilities, or take a closer look at the skills you saved."}
                      </p>
                      <button
                        className="primary-button"
                        disabled={busy}
                        onClick={() => void startMatch(!!session)}
                      >
                        {session?.status === "failed" ||
                        session?.status === "cancelled"
                          ? "Try again"
                          : "Find more possibilities"}
                        <ArrowRight size={17} />
                      </button>
                      <button
                        className="text-button"
                        onClick={() =>
                          changeView(saved.length ? "saved" : "explore")
                        }
                      >
                        {saved.length
                          ? "See my saved skills"
                          : "Explore the catalog"}
                        <ArrowUpRight size={15} />
                      </button>
                    </div>
                  )}
                </div>
                {!matching && !session && previewSkill ? (
                  <div className="preview-controls">
                    <button
                      className="preview-arrow"
                      aria-label="Previous preview skill"
                      onClick={() =>
                        setPreviewIndex(
                          (index) =>
                            (index - 1 + previews.length) % previews.length,
                        )
                      }
                    >
                      <ArrowLeft size={17} />
                    </button>
                    <span>Just a little taste of what’s out there</span>
                    <button
                      className="preview-arrow"
                      aria-label="Next preview skill"
                      onClick={() => setPreviewIndex((index) => index + 1)}
                    >
                      <ArrowRight size={17} />
                    </button>
                  </div>
                ) : !matching && currentSkill ? (
                  <>
                    <div className="swipe-controls">
                      <button
                        className="undo-button"
                        disabled={busy || !lastChoice}
                        onClick={() => void undo()}
                        aria-label="Undo last choice"
                      >
                        <RotateCcw size={19} />
                      </button>
                      <button
                        className="pass-button"
                        disabled={busy}
                        onClick={() => void choose("pass")}
                        aria-label="Pass on this skill"
                      >
                        <X size={27} strokeWidth={1.7} />
                      </button>
                      <button
                        className="save-button"
                        disabled={busy}
                        onClick={() => void choose("save")}
                        aria-label="Save this skill"
                      >
                        <Heart size={26} strokeWidth={1.8} />
                      </button>
                      <button
                        className="reason-button"
                        disabled={busy}
                        onClick={() => setShowPassReasons((value) => !value)}
                        aria-label="Pass with a reason"
                        aria-expanded={showPassReasons}
                      >
                        <SlidersHorizontal size={19} />
                      </button>
                    </div>
                    <div className="swipe-hint">
                      <span>← Pass</span>
                      <span>Swipe to follow your instinct</span>
                      <span>Save →</span>
                    </div>
                    {showPassReasons && (
                      <div className="pass-reasons">
                        <span>What didn’t click?</span>
                        {(
                          [
                            ["wrong-task", "Not this task"],
                            ["wrong-style", "Not my style"],
                            ["too-much-setup", "Too much setup"],
                          ] as const
                        ).map(([reason, label]) => (
                          <button
                            key={reason}
                            disabled={busy}
                            onClick={() => void choose("pass", reason)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                    )}
                  </>
                ) : !matching && lastChoice ? (
                  <button
                    className="text-button finished-undo"
                    disabled={busy}
                    onClick={() => void undo()}
                  >
                    <RotateCcw size={15} />
                    Undo last choice
                  </button>
                ) : null}
              </div>
            </section>

            <section className="inspiration-section">
              <div className="section-line">
                <div>
                  <span className="eyebrow">A FEW PLACES TO START</span>
                  <h2>What’s on your mind?</h2>
                </div>
                <button
                  className="text-button"
                  onClick={() => changeView("explore")}
                >
                  Explore all {skills.length} skills
                  <ArrowUpRight size={16} />
                </button>
              </div>
              <div className="inspiration-grid">
                {examples.map((example) => (
                  <button
                    key={example.label}
                    onClick={() => {
                      setTask(example.task);
                      taskRef.current?.focus();
                      window.scrollTo({ top: 0, behavior: "smooth" });
                    }}
                    disabled={matching}
                  >
                    <span
                      className="inspiration-icon"
                      data-category={example.category}
                    >
                      <SkillIcon category={example.category} size={22} />
                    </span>
                    <span>{example.label}</span>
                    <ArrowUpRight size={17} />
                  </button>
                ))}
              </div>
            </section>
          </>
        ) : (
          <section className="library-view">
            <div className="library-heading">
              <div>
                <span className="eyebrow">
                  {view === "saved"
                    ? "THE ONES THAT CLICKED"
                    : "A WORLD OF WAYS TO WORK"}
                </span>
                <h1>
                  {view === "saved"
                    ? "Your kind of capable."
                    : "Good skills. Open minds."}
                </h1>
                <p className="muted">
                  {view === "saved"
                    ? "Your shortlist, ready for whatever comes next."
                    : `${skills.length} real skills from people with different ways of doing great work.`}
                </p>
              </div>
              <div className="library-count">
                <span>{view === "saved" ? saved.length : skills.length}</span>
                {view === "saved" ? "saved skills" : "possibilities"}
              </div>
            </div>
            <div className="library-tools">
              <label className="search-input">
                <Search size={18} />
                <input
                  aria-label="Search skills"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="A skill, a task, a different approach…"
                />
                {search && (
                  <button
                    aria-label="Clear search"
                    onClick={() => setSearch("")}
                  >
                    <X size={16} />
                  </button>
                )}
              </label>
              <div className="category-select">
                <select
                  aria-label="Filter by category"
                  value={category}
                  onChange={(event) => setCategory(event.target.value)}
                >
                  <option>All skills</option>
                  {categories.map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
                <ChevronDown size={16} />
              </div>
            </div>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            {visibleSkills.length ? (
              <div className="library-grid">
                {visibleSkills.map((skill) => (
                  <article className="library-card" key={skill.id}>
                    <div className="section-line">
                      <div
                        className="skill-symbol"
                        data-category={skill.category}
                      >
                        <SkillIcon category={skill.category} size={25} />
                      </div>
                      <span
                        className="category-label"
                        data-category={skill.category}
                      >
                        {skill.category}
                      </span>
                    </div>
                    <button
                      className="library-card-link"
                      onClick={() => setDetail(skill)}
                    >
                      <h2>
                        {skill.name}
                        <ArrowUpRight size={18} />
                      </h2>
                      <p>{skill.description}</p>
                    </button>
                    <div className="tag-row">
                      {skill.tags.slice(0, 2).map((tag) => (
                        <span key={tag}>{tag}</span>
                      ))}
                    </div>
                    <div className="library-card-bottom">
                      <span>
                        by <strong>{skill.author}</strong>
                      </span>
                      {view === "saved" ? (
                        <button
                          className="icon-button saved-icon"
                          onClick={() => void removeSaved(skill)}
                          aria-label={`Remove ${skill.name} from saved`}
                        >
                          <Bookmark size={18} fill="currentColor" />
                        </button>
                      ) : (
                        <button
                          className="text-button"
                          onClick={() => setDetail(skill)}
                        >
                          Take a look
                          <ArrowRight size={15} />
                        </button>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div className="library-empty">
                <div className="empty-icon">
                  {view === "saved" && !saved.length ? (
                    <Heart size={30} />
                  ) : (
                    <Search size={30} />
                  )}
                </div>
                <h2>
                  {view === "saved" && !saved.length
                    ? "Make room for a good match."
                    : "No skills found."}
                </h2>
                <p>
                  {view === "saved" && !saved.length
                    ? "Swipe right on a skill you like. It’ll be waiting right here."
                    : "Try another search or give a different category a look."}
                </p>
                <button
                  className="primary-button"
                  onClick={() => {
                    if (view === "saved" && !saved.length)
                      changeView("discover");
                    else {
                      setSearch("");
                      setCategory("All skills");
                    }
                  }}
                >
                  {view === "saved" && !saved.length
                    ? "Find my first match"
                    : "Show all skills"}
                  <ArrowRight size={17} />
                </button>
              </div>
            )}
          </section>
        )}
      </main>

      <footer className="site-footer">
        <div>
          <BrandMark small />
          <span>A little less searching. A little more possibility.</span>
        </div>
        <span>
          Skills, meet chemistry.<span className="footer-heart"> ♡</span>
        </span>
      </footer>
      {toast && (
        <div className="toast" role="status">
          <Check size={16} />
          {toast}
          <button
            aria-label="Dismiss notification"
            onClick={() => setToast("")}
          >
            <X size={14} />
          </button>
        </div>
      )}
      {preferencesOpen && (
        <Preferences
          profile={{ ...profile, agent }}
          onClose={() => setPreferencesOpen(false)}
          onSave={async (next) => {
            const result = await api.saveProfile(next);
            setProfile(result);
            setToast(
              "Your taste is saved. We’ll use it for your next matches.",
            );
          }}
        />
      )}
      {detail && (
        <SkillDetail
          skill={detail}
          agent={agent}
          recommendation={session?.recommendations.find(
            (item) => item.skill_id === detail.id,
          )}
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  );
}
