import { useState } from "react";
import {
  ArrowUpRight,
  Check,
  Copy,
  ExternalLink,
  Quote,
  Terminal,
  CircleCheck,
  Bookmark,
} from "lucide-react";
import type { Agent, Recommendation, Skill } from "../lib/types";
import Dialog from "./Dialog";
import { SkillIcon } from "./Icons";

export default function SkillDetail({
  skill,
  agent: initialAgent,
  recommendation,
  onClose,
  saved,
  saveDisabled,
  onSave,
  saveError,
}: {
  skill: Skill;
  agent: Agent;
  recommendation?: Recommendation;
  onClose: () => void;
  saved: boolean;
  saveDisabled: boolean;
  onSave: () => void;
  saveError: { message: string; label: string; retry: () => void } | null;
}) {
  // Installer selection belongs to this dialog and must not change the active task's agent.
  const [agent, setAgent] = useState<Agent>(
    skill.compatibility?.includes(initialAgent)
      ? initialAgent
      : (skill.compatibility?.[0] ?? initialAgent),
  );
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  // Imported installation commands are unverified; direct users to the original source.
  const command = skill.provenance === "skillsmp" ? null : skill.install_commands[agent];
  async function copy() {
    if (!command) return;
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setError("");
    } catch {
      setError(
        "Copy is unavailable in this browser. Select and copy the command below.",
      );
    }
  }
  return (
    <Dialog title="Skill details" onClose={onClose} className="detail-dialog">
      <h3>{skill.name}</h3>
      {skill.category !== "Uncategorized" && (
        <div className="detail-meta">
          <SkillIcon category={skill.category} size={16} />
          <span>{skill.category}</span>
        </div>
      )}
      <p className="detail-description">{skill.description}</p>
      <button
        className="primary-button detail-save"
        disabled={saveDisabled}
        onClick={onSave}
        aria-label={
          saved ? `Remove ${skill.name} from saved` : `Save ${skill.name}`
        }
      >
        {saved ? (
          <Check size={18} aria-hidden="true" />
        ) : (
          <Bookmark size={18} aria-hidden="true" />
        )}{" "}
        {saved ? "Remove from saved" : "Save skill"}
      </button>
      {saveError && (
        <div className="error-notice" role="alert">
          <p>{saveError.message}</p>
          <button disabled={saveDisabled} onClick={saveError.retry}>
            {saveError.label}
          </button>
        </div>
      )}
      <a
        className="source-line"
        href={skill.repository ? `https://github.com/${skill.repository}` : skill.source_url}
        target="_blank"
        rel="noreferrer"
      >
        {skill.repository || "Original source"}
        <ArrowUpRight size={15} aria-hidden="true" />
      </a>
      {recommendation && (
        <div className="detail-match">
          <h3>Why this skill fits</h3>
          <p>
            <strong>Your task</strong>
            {recommendation.task_fit}
          </p>
          <p>
            <strong>Your taste</strong>
            {recommendation.taste_fit}
          </p>
          {recommendation.caveat && (
            <p className="match-caveat">{recommendation.caveat}</p>
          )}
        </div>
      )}
      <div className="detail-columns">
        <section>
          <h3>How it works</h3>
          {skill.workflow.length ? <ul>
            {skill.workflow.map((item) => (
              <li key={item}>
                <CircleCheck size={14} aria-hidden="true" />
                {item}
              </li>
            ))}
          </ul> : <p className="muted small">The workflow has not been verified. Check the source for details.</p>}
        </section>
        <section>
          <h3>What to expect</h3>
          {skill.output_style.length > 0 ? (
            <ul>
              {skill.output_style.map((item) => (
                <li key={item}>
                  <CircleCheck size={14} aria-hidden="true" />
                  {item}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">
              {skill.provenance === "skillsmp"
                ? "Output style has not been verified."
                : "The source does not specify an output style."}
            </p>
          )}
        </section>
      </div>
      {skill.requirements.length > 0 && (
        <section className="detail-requirements">
          <h3>Before you get started</h3>
          <ul>
            {skill.requirements.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      )}
      {!skill.requirements.length && skill.provenance === "skillsmp" && (
        <p className="small muted">Setup requirements have not been verified.</p>
      )}
      <section className="install-section">
        <div className="section-line">
          <h3>
            <Terminal size={17} aria-hidden="true" />
            Installation command
          </h3>
          {skill.compatibility?.length ? <div className="mini-segment">
            {(["codex", "claude-code"] as const).map((target) => (
              <button
                key={target}
                aria-pressed={agent === target}
                disabled={!skill.compatibility?.includes(target)}
                onClick={() => {
                  setAgent(target);
                  setCopied(false);
                  setError("");
                }}
              >
                {target === "codex" ? "Codex" : "Claude Code"}
              </button>
            ))}
          </div> : <span className="small muted">Compatibility not verified</span>}
        </div>
        {command ? (
          <>
            <p className="small muted">
              Run this in the project where you want to use the skill. The
              installer will walk you through the next steps.
            </p>
            <div className="command-box">
              <code>{command}</code>
              <button
                className="icon-button"
                onClick={copy}
                aria-label="Copy installation command"
              >
                {copied ? (
                  <Check size={18} aria-hidden="true" />
                ) : (
                  <Copy size={18} aria-hidden="true" />
                )}
              </button>
            </div>
            <span className="copy-status" role="status">
              {copied
                ? "Command copied. Ready when you are."
                : error ||
                  "This copies a command; nothing is installed by Skinder."}
            </span>
          </>
        ) : (
          <p className="small muted">
            Follow the author’s setup instructions in the source.
          </p>
        )}
      </section>
      {skill.evidence.length > 0 && (
        <details className="evidence">
          <summary>
            <Quote size={15} aria-hidden="true" />
            What the source says<span>Style traits are inferred</span>
          </summary>
          {skill.evidence.map((item, index) => (
            <blockquote key={index}>
              <strong>{item.trait}</strong>“{item.excerpt}”
            </blockquote>
          ))}
        </details>
      )}
      {!skill.evidence.length && (
        <p className="small muted">No reviewed working-style traits are recorded.</p>
      )}
      <div className="detail-footer">
        <span className="detail-meta">
          {skill.license || "License unknown"}. {skill.revision ? `Source ${skill.revision.slice(0, 7)}` : "Source revision unknown"}
        </span>
        <a href={skill.source_url} target="_blank" rel="noreferrer">
          Read the skill
          <ExternalLink size={14} aria-hidden="true" />
        </a>
        {skill.provider_url && (
          <a href={skill.provider_url} target="_blank" rel="noreferrer">View on SkillsMP <ExternalLink size={14} aria-hidden="true" /></a>
        )}
      </div>
    </Dialog>
  );
}
