import { useRef, useState, type CSSProperties } from "react";
import { ArrowUpRight } from "lucide-react";
import type { Recommendation, Skill } from "../lib/types";

export default function SkillCard({
  skill,
  recommendation,
  disabled,
  onOpen,
  onSwipe,
}: {
  skill: Skill;
  recommendation?: Recommendation;
  disabled?: boolean;
  onOpen: () => void;
  onSwipe: (action: "save" | "pass") => void;
}) {
  const start = useRef<{ x: number; y: number; pointerId: number } | null>(null);
  const [drag, setDrag] = useState(0);
  function reset() {
    start.current = null;
    setDrag(0);
  }
  return (
    <article
      className="skill-card"
      style={
        {
          "--drag": `${drag}px`,
          "--rotation": `${drag / 35}deg`,
        } as CSSProperties
      }
      onPointerDown={(event) => {
        if (
          disabled ||
          start.current ||
          event.button !== 0 ||
          (event.target as HTMLElement).closest(
            "button,a,input,select,textarea",
          )
        )
          return;
        start.current = {
          x: event.clientX,
          y: event.clientY,
          pointerId: event.pointerId,
        };
      }}
      onPointerMove={(event) => {
        if (
          !start.current ||
          start.current.pointerId !== event.pointerId ||
          disabled
        ) return;
        const dx = event.clientX - start.current.x;
        const dy = event.clientY - start.current.y;
        if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 10) {
          reset();
          return;
        }
        if (Math.abs(dx) > 12) {
          event.currentTarget.setPointerCapture(event.pointerId);
          setDrag(Math.max(-75, Math.min(75, dx)));
        }
      }}
      onPointerUp={(event) => {
        if (!start.current || start.current.pointerId !== event.pointerId) return;
        const dx = event.clientX - start.current.x;
        const dy = event.clientY - start.current.y;
        reset();
        if (
          !disabled &&
          Math.abs(dx) > 100 &&
          Math.abs(dx) > Math.abs(dy) * 1.5
        )
          onSwipe(dx > 0 ? "save" : "pass");
      }}
      onPointerCancel={(event) => {
        if (start.current?.pointerId === event.pointerId) reset();
      }}
      onLostPointerCapture={(event) => {
        if (start.current?.pointerId === event.pointerId) reset();
      }}
    >
      <div className="card-topline">
        <span className="card-index" aria-hidden="true">
          K<br />♦
        </span>
        <span>{skill.category}</span>
      </div>
      <div className="card-content">
        <h2>{skill.name}</h2>
        <p className="card-author">
          by <strong>{skill.author}</strong>
        </p>
        <p className="card-description">{skill.description}</p>
        <div className="fit-section">
          <h3>Fits your task</h3>
          <p>{recommendation?.task_fit}</p>
        </div>
        <div className="fit-section">
          <h3>Fits your style</h3>
          <p>{recommendation?.taste_fit}</p>
        </div>
        {recommendation?.caveat && (
          <p className="card-caveat">{recommendation.caveat}</p>
        )}
        <div className="card-source">
          <span className="small muted">
            {skill.compatibility
              .map((agent) => (agent === "codex" ? "Codex" : "Claude Code"))
              .join(" · ")}
          </span>
          <button className="text-button" onClick={onOpen}>
            View skill &amp; source <ArrowUpRight size={17} />
          </button>
        </div>
      </div>
      <span className="card-index card-index-bottom" aria-hidden="true">
        K<br />♦
      </span>
      {Math.abs(drag) > 35 && (
        <span
          className={`swipe-stamp ${drag > 0 ? "save" : "pass"}`}
          aria-hidden="true"
        >
          {drag > 0 ? "SAVE" : "PASS"}
        </span>
      )}
    </article>
  );
}
