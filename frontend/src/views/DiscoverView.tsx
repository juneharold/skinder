import type { ReactNode, RefObject } from "react";
import { ArrowLeft, ArrowRight, ArrowUpRight, Pencil, RotateCcw, SlidersHorizontal } from "lucide-react";
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
  const recommendation = remaining[0];
  const currentSkill = recommendation ? skillsById[recommendation.skill_id] : undefined;
  const total = session?.recommendations.length ?? 0;
  const reviewed = total - remaining.length;
  const knownSkills = Object.values(skillsById);
  const hasMoreCandidates = catalogSize === undefined || catalogSize > knownSkills.length || knownSkills.some(
    (skill) => session &&
      (skill.compatibility === null || skill.compatibility.includes(session.agent)) &&
      !reviewedIds.has(skill.id),
  );
  const retryMatching = session?.status === "failed" || session?.status === "cancelled";
  const canContinue = total > 0 && hasMoreCandidates;
  function choose(action: "save" | "pass", reason: PassReason | null = null) {
    if (currentSkill) matches.choose(currentSkill, action, reason);
  }
  return (
    <>
      {profileNotice}
      <LoadNotice label="your current task" retryLabel="session" state={matches.loadState} error={matches.loadError} onRetry={matches.reload} />
      {matches.loadState === "loading" ? (
        <div className="empty-state" role="status">
          <span className="diamond">♦</span>
          <h1>Loading your table…</h1>
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
              href={exploreHref}
              onClick={(event) => {
                event.preventDefault();
                onNavigate("explore");
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
              <span className="eyebrow">Target</span>
              <strong>
                {session.agent === "codex" ? "Codex" : "Claude Code"}
              </strong>
            </div>
            <div className="sidebar-actions">
              <button
                disabled={preferencesDisabled}
                onClick={onPreferences}
              >
                <SlidersHorizontal size={18} />
                Preferences
              </button>
              <button
                disabled={matchBusy || matching || writeBusy}
                onClick={onEditTask}
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
                  !recommendation && (
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
                        onClick={retryMatching || canContinue ? more : onEditTask}
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
                        onClick={() => onNavigate(total ? "saved" : "explore")}
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
  );
}
