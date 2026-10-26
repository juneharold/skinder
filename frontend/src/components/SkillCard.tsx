import { useRef, useState, type CSSProperties } from "react";
import { ArrowUpRight, Check, Heart, Sparkles, Target } from "lucide-react";
import type { Recommendation, Skill } from "../lib/types";
import { SkillIcon } from "./Icons";

export default function SkillCard({
  skill,
  recommendation,
  preview,
  disabled,
  departing,
  onOpen,
  onSwipe,
}: {
  skill: Skill;
  recommendation?: Recommendation;
  preview?: boolean;
  disabled?: boolean;
  departing?: "save" | "pass" | null;
  onOpen: () => void;
  onSwipe: (action: "save" | "pass") => void;
}) {
  const start = useRef<{ x: number; y: number } | null>(null);
  const [drag, setDrag] = useState(0);
  const Icon = preview ? Sparkles : Target;
  return (
    <article
      className={`skill-card ${preview ? "preview-card" : ""} ${departing ? `departing-${departing}` : ""}`}
      data-category={skill.category}
      style={
        {
          "--drag": `${drag}px`,
          "--rotation": `${drag / 22}deg`,
        } as CSSProperties
      }
      onPointerDown={(event) => {
        if (
          preview ||
          disabled ||
          event.button !== 0 ||
          (event.target as HTMLElement).closest("button,a")
        )
          return;
        start.current = { x: event.clientX, y: event.clientY };
      }}
      onPointerMove={(event) => {
        if (!start.current || disabled) return;
        const dx = event.clientX - start.current.x;
        const dy = event.clientY - start.current.y;
        if (Math.abs(dy) > Math.abs(dx) + 25) {
          start.current = null;
          setDrag(0);
          return;
        }
        if (Math.abs(dx) > 8) {
          event.currentTarget.setPointerCapture(event.pointerId);
          setDrag(dx);
        }
      }}
      onPointerUp={(event) => {
        if (!start.current) return;
        const delta = event.clientX - start.current.x;
        start.current = null;
        setDrag(0);
        if (!disabled && Math.abs(delta) > 100)
          onSwipe(delta > 0 ? "save" : "pass");
      }}
      onPointerCancel={() => {
        start.current = null;
        setDrag(0);
      }}
    >
      {Math.abs(drag) > 40 && (
        <div className={`swipe-stamp ${drag > 0 ? "save" : "pass"}`}>
          {drag > 0 ? "IT’S A YES" : "MAYBE NEXT TIME"}
        </div>
      )}
      <div className="card-topline">
        <span className="category-label" data-category={skill.category}>
          <span />
          {skill.category}
        </span>
        <span className="card-top-note">
          {preview ? "MEET THE POSSIBILITIES" : "PICKED FOR YOUR TASK"}
        </span>
      </div>
      <div className="card-main">
        <div className="skill-symbol" data-category={skill.category}>
          <SkillIcon category={skill.category} size={36} />
        </div>
        <h2>{skill.name}</h2>
        <p className="card-description">{skill.description}</p>
        <div className="tag-row">
          {skill.tags.slice(0, 3).map((tag) => (
            <span key={tag}>{tag}</span>
          ))}
        </div>
      </div>
      <div className="fit-section">
        <div className="fit-row">
          <span className="fit-icon">
            <Icon size={16} />
          </span>
          <div>
            <h3>
              {preview ? "A skill with a point of view" : "Good for your task"}
            </h3>
            <p>
              {recommendation?.task_fit ||
                skill.workflow[0] ||
                "A focused approach, straight from the skill’s instructions."}
            </p>
          </div>
        </div>
        <div className="fit-row">
          <span className="fit-icon heart">
            <Heart size={16} />
          </span>
          <div>
            <h3>
              {preview
                ? "More than a name and a description"
                : "Your kind of approach"}
            </h3>
            <p>
              {recommendation?.taste_fit ||
                skill.output_style[0] ||
                "Look inside to see how this skill approaches its work."}
            </p>
          </div>
        </div>
      </div>
      {recommendation?.caveat && (
        <p className="card-caveat">{recommendation.caveat}</p>
      )}
      <div className="card-bottom">
        <span className="card-author">
          <span className="author-avatar">
            {skill.author.slice(0, 1).toUpperCase()}
          </span>
          <span>
            by <strong>{skill.author}</strong>
          </span>
        </span>
        <button className="text-button" onClick={onOpen}>
          Meet the skill
          <ArrowUpRight size={16} />
        </button>
      </div>
      <div className="compatibility">
        <Check size={12} />
        <span>
          {skill.compatibility.length === 2
            ? "Codex & Claude Code"
            : skill.compatibility.includes("codex")
              ? "Codex"
              : "Claude Code"}
        </span>
        <span className="dot" />
        <span>Real skill. Original source.</span>
      </div>
    </article>
  );
}
