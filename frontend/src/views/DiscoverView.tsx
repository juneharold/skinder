import type { ReactNode, RefObject } from "react";
import { Pencil, RotateCcw, SlidersHorizontal } from "lucide-react";
import SkillCard from "../components/SkillCard";
import LoadNotice from "../components/LoadNotice";
import type { PassReason, Skill } from "../lib/types";
import type { MatchSessionController } from "../hooks/useMatchSession";

type DiscoverViewProps = {
  matches: MatchSessionController;
  skillsById: Record<string, Skill>;
  catalogSize?: number;
  skillLoadError: string;
  onRetrySkills: () => void;
  taskForm: ReactNode;
  matchNotice: ReactNode;
  profileNotice: ReactNode;
  preferencesDisabled: boolean;
  onPreferences: () => void;
  onEditTask: () => void;
  onOpenSkill: (skill: Skill) => void;
  onNavigate: (view: "explore" | "saved") => void;
  exploreHref: string;
  deckRef: RefObject<HTMLDivElement | null>;
  shortcutsDisabled: boolean;
  editOpen: boolean;
  showPassReasons: boolean;
  onTogglePassReasons: () => void;
};

export default function DiscoverView({
  matches, skillsById, catalogSize, skillLoadError, onRetrySkills,
  taskForm, matchNotice, profileNotice, preferencesDisabled, onPreferences,
  onEditTask, onOpenSkill, onNavigate, exploreHref, deckRef,
  shortcutsDisabled, editOpen, showPassReasons, onTogglePassReasons,
}: DiscoverViewProps) {
  const { session, matching, starting: matchBusy, cancelling: cancelBusy,
    canMatch, pollError, remaining, reviewedIds, lastChoice, more, undo, cancel } = matches;
  const writeBusy = matches.saved.busy;
  const savedIds = new Set(matches.saved.items.map((item) => item.skill.id));
  // Recommendation IDs survive catalog filtering; missing details do not mean an empty deck.
  const recommendation = remaining[0];
  const currentSkill = recommendation ? skillsById[recommendation.skill_id] : undefined;
  const total = session?.recommendations.length ?? 0;
  const reviewed = total - remaining.length;
  const knownSkills = Object.values(skillsById);
  // Cached pages may be incomplete, and unknown compatibility remains eligible on the server.
  // Offer another batch unless the known catalog proves exhaustion.
  const hasMoreCandidates = catalogSize === undefined || catalogSize > knownSkills.length || knownSkills.some(
    (skill) => session &&
      (skill.compatibility === null || skill.compatibility.includes(session.agent)) &&
      !reviewedIds.has(skill.id),
  );
  const retryMatching = session?.status === "failed" || session?.status === "cancelled";
  const canContinue = total > 0 && hasMoreCandidates;
  let emptyHeading = "No matching skills yet.";
  if (session?.status === "cancelled") {
    emptyHeading = "Matching cancelled.";
  } else if (session?.status === "failed") {
    emptyHeading = "Matching could not finish.";
  } else if (total && hasMoreCandidates) {
    emptyHeading = "Batch reviewed.";
  } else if (total) {
    emptyHeading = "All available skills reviewed.";
  } else if (session && session.feedback.length > 0) {
    emptyHeading = "No further matches.";
  }
  function choose(action: "save" | "pass", reason: PassReason | null = null) {
    if (currentSkill) matches.choose(currentSkill, action, reason);
  }
  return (
    <>
      {profileNotice}
      <LoadNotice label="your current task" retryLabel="session" state={matches.loadState} error={matches.loadError} onRetry={matches.reload} />
      {matches.loadState === "loading" ? (
        <div className="empty-state" role="status">
          <h1>Loading your task…</h1>
        </div>
      ) : matches.loadState === "error" ? (
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
            <h1>Find skills for your task</h1>
            <p>
              Describe your task, then review skills that fit how you work.
            </p>
          </div>
          <div className="task-entry">
            {taskForm}
            {matchNotice}
          </div>
          <a
            className="explore-link"
            href={exploreHref}
            onClick={(event) => {
              event.preventDefault();
              onNavigate("explore");
            }}
          >
            Explore the catalog
          </a>
        </section>
      ) : (
        <div className="discover-layout">
          <aside className="task-sidebar">
            <h1 className={session.task.length > 80 ? "long-task" : undefined}>
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
              <span className="field-label">Target agent</span>
              <strong>
                {session.agent === "codex" ? "Codex" : "Claude Code"}
              </strong>
            </div>
            <div className="sidebar-actions">
              <button
                disabled={preferencesDisabled}
                onClick={onPreferences}
              >
                <SlidersHorizontal size={18} aria-hidden="true" />
                Preferences
              </button>
              <button
                disabled={matchBusy || matching || writeBusy}
                onClick={onEditTask}
              >
                <Pencil size={18} aria-hidden="true" />
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
              // Shortcuts belong to the focused deck, without overriding typing or modal controls.
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
                shortcutsDisabled
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
                <h2>{session.status === "queued" ? "Your task is queued…" : "Finding skills…"}</h2>
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
                        disabled={!canMatch() || writeBusy}
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
                {!currentSkill && recommendation && skillLoadError ? (
                  <div className="error-notice" role="alert">
                    <p>{skillLoadError}</p>
                    <button onClick={() => onRetrySkills()}>Retry skills</button>
                  </div>
                ) : !currentSkill && recommendation ? (
                  <div className="empty-state" role="status">
                    Loading skills…
                  </div>
                ) : currentSkill ? (
                  <div className="card-stack">
                    <SkillCard
                      skill={currentSkill}
                      recommendation={recommendation}
                      disabled={writeBusy || matchBusy}
                      onOpen={() => onOpenSkill(currentSkill)}
                      onSwipe={choose}
                    />
                    <div className="deck-actions">
                      <button
                        aria-label="Undo last choice"
                        disabled={!lastChoice || writeBusy}
                        onClick={undo}
                      >
                        <RotateCcw size={18} aria-hidden="true" />
                        <span>Undo</span>
                      </button>
                      <button
                        aria-label="Pass on this skill"
                        disabled={writeBusy}
                        onClick={() => choose("pass")}
                      >
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
                      </button>
                    </div>
                    <div className="pass-reasons">
                      <button
                        className="text-button"
                        disabled={writeBusy}
                        aria-expanded={showPassReasons}
                        onClick={() =>
                          onTogglePassReasons()
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
                  <div className="empty-state" role="status">
                    <h2>{emptyHeading}</h2>
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
                        ((retryMatching || canContinue) && !canMatch())
                      }
                      onClick={retryMatching || canContinue ? more : onEditTask}
                    >
                      {retryMatching
                        ? "Retry matching"
                        : canContinue
                          ? "Find more for this task"
                          : "Change task"}
                    </button>
                    <button
                      className="text-button"
                      onClick={() => onNavigate(total ? "saved" : "explore")}
                    >
                      {total
                        ? "View saved skills"
                        : "Explore the catalog"}
                    </button>
                    {lastChoice && (
                      <button
                        className="text-button"
                        aria-label="Undo last choice"
                        disabled={writeBusy}
                        onClick={undo}
                      >
                        <RotateCcw size={16} aria-hidden="true" />
                        Undo last choice
                      </button>
                    )}
                  </div>
                )}
              </>
            )}
          </section>
          <aside className="progress-sidebar">
            <div>
              <p className="progress-count">
                {matching && !total ? (
                  <span>Preparing your deck</span>
                ) : (
                  <>{reviewed} <span>of {total} reviewed</span></>
                )}
              </p>
            </div>
            <div className="shortcuts">
              <h2>Keyboard shortcuts</h2>
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
  );
}
