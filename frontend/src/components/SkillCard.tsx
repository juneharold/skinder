import { useRef, useState, type CSSProperties } from "react";
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
  // Only the initiating pointer owns the gesture; another touch must not move or commit it.
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
        // Leave vertical gestures to page scrolling instead of capturing the pointer.
        if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 10) {
          reset();
          return;
        }
        // Clamp the visual movement without reducing the full distance used to commit a swipe.
        if (Math.abs(dx) > 12) {
          event.currentTarget.setPointerCapture(event.pointerId);
          setDrag(Math.max(-75, Math.min(75, dx)));
        }
      }}
      onPointerUp={(event) => {
        if (!start.current || start.current.pointerId !== event.pointerId) return;
        const dx = event.clientX - start.current.x;
        const dy = event.clientY - start.current.y;
        // Clear this gesture before the action can replace the card with the next skill.
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
      <div className="card-content">
        <h2>{skill.name}</h2>
        <p className="card-author">
          by <strong>{skill.author}</strong>
        </p>
        {skill.category !== "Uncategorized" && <p className="category-label">{skill.category}</p>}
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
            {skill.compatibility?.length
              ? skill.compatibility.map((agent) => (agent === "codex" ? "Codex" : "Claude Code")).join(", ")
              : "Compatibility not verified"}
          </span>
          <button className="text-button" onClick={onOpen}>
            View skill &amp; source
          </button>
        </div>
      </div>
      {Math.abs(drag) > 35 && (
        <span
          className={`swipe-stamp ${drag > 0 ? "save" : "pass"}`}
          aria-hidden="true"
        >
          {drag > 0 ? "Save" : "Pass"}
        </span>
      )}
    </article>
  );
}
