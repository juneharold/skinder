import { useState } from "react";
import {
  ArrowUpRight,
  Check,
  Copy,
  ExternalLink,
  Quote,
  Terminal,
  CircleCheck,
} from "lucide-react";
import type { Agent, Recommendation, Skill } from "../lib/types";
import { SkillIcon } from "./Icons";
import Dialog from "./Dialog";

export default function SkillDetail({
  skill,
  agent: initialAgent,
  recommendation,
  onClose,
}: {
  skill: Skill;
  agent: Agent;
  recommendation?: Recommendation;
  onClose: () => void;
}) {
  const [agent, setAgent] = useState<Agent>(
    skill.compatibility.includes(initialAgent)
      ? initialAgent
      : (skill.compatibility[0] ?? initialAgent),
  );
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const command = skill.install_commands[agent];
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
    <Dialog title="Meet the skill" onClose={onClose} className="detail-dialog">
      <div className="detail-identity">
        <div className="skill-symbol" data-category={skill.category}>
          <SkillIcon category={skill.category} size={34} />
        </div>
        <span className="category-label" data-category={skill.category}>
          {skill.category}
        </span>
      </div>
      <h2>{skill.name}</h2>
      <p className="detail-description">{skill.description}</p>
      <a
        className="source-line"
        href={`https://github.com/${skill.repository}`}
        target="_blank"
        rel="noreferrer"
      >
        <span className="author-avatar">
          {skill.author.slice(0, 1).toUpperCase()}
        </span>
        {skill.repository}
        <ArrowUpRight size={15} />
      </a>
      {recommendation && (
        <div className="detail-match">
          <h3>Why you two might click</h3>
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
          <ul>
            {skill.workflow.map((item) => (
              <li key={item}>
                <CircleCheck size={14} />
                {item}
              </li>
            ))}
          </ul>
        </section>
        <section>
          <h3>What to expect</h3>
          {skill.output_style.length > 0 ? (
            <ul>
              {skill.output_style.map((item) => (
                <li key={item}>
                  <CircleCheck size={14} />
                  {item}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">
              The source does not specify an output style.
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
      <section className="install-section">
        <div className="section-line">
          <h3>
            <Terminal size={17} />
            Take it for a spin
          </h3>
          <div className="mini-segment">
            {(["codex", "claude-code"] as const).map((target) => (
              <button
                key={target}
                aria-pressed={agent === target}
                disabled={!skill.compatibility.includes(target)}
                onClick={() => {
                  setAgent(target);
                  setCopied(false);
                  setError("");
                }}
              >
                {target === "codex" ? "Codex" : "Claude Code"}
              </button>
            ))}
          </div>
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
                {copied ? <Check size={18} /> : <Copy size={18} />}
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
            <Quote size={15} />
            What the source says<span>Style traits are inferred</span>
          </summary>
          {skill.evidence.map((item, index) => (
            <blockquote key={index}>
              <strong>{item.trait}</strong>“{item.excerpt}”
            </blockquote>
          ))}
        </details>
      )}
      <div className="detail-footer">
        <span>
          {skill.license} · Source {skill.revision.slice(0, 7)}
        </span>
        <a href={skill.source_url} target="_blank" rel="noreferrer">
          Read the skill
          <ExternalLink size={14} />
        </a>
      </div>
    </Dialog>
  );
}
