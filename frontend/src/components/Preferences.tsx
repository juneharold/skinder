import { useEffect, useRef, useState } from "react";
import { ArrowRight, Check } from "lucide-react";
import type { Profile } from "../lib/types";
import Dialog from "./Dialog";

export default function Preferences({
  profile,
  onSave,
  onClose,
}: {
  profile: Profile;
  onSave: (profile: Profile) => Promise<void>;
  onClose: () => void;
}) {
  // Keep edits local to this opening; failed saves must not reset them from profile props.
  const [draft, setDraft] = useState(profile);
  const pending = useRef(false);
  // Closing leaves the parent's save running; do not update this dialog after unmount.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const groups = [
    {
      field: "planning",
      title: "How should it get started?",
      options: [
        ["plan-first", "Think it through"],
        ["balanced", "It depends"],
        ["act-first", "Get to work"],
      ],
    },
    {
      field: "communication",
      title: "How much explanation feels right?",
      options: [
        ["concise", "Keep it short"],
        ["balanced", "A good balance"],
        ["detailed", "Show the details"],
      ],
    },
    {
      field: "verification",
      title: "How should it check its work?",
      options: [
        ["pragmatic", "Keep it practical"],
        ["balanced", "A good balance"],
        ["thorough", "Be thorough"],
      ],
    },
  ] as const;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    pending.current = true;
    setSaving(true);
    setError("");
    try {
      await onSave(draft);
    } catch (error) {
      if (mounted.current)
        setError(
          error instanceof Error
            ? error.message
            : "Your preferences could not be saved.",
        );
    } finally {
      pending.current = false;
      if (mounted.current) setSaving(false);
    }
  }
  return (
    <Dialog
      title="Preferences"
      onClose={onClose}
      className="preferences-dialog"
    >
      <h2>Make it your kind of work.</h2>
      <p className="muted">
        The right skill should fit how you like to work, too. Everything here is
        optional.
      </p>
      <form onSubmit={submit}>
        {groups.map((group) => (
          <fieldset
            disabled={saving}
            className="preference-group"
            key={group.field}
          >
            <legend>{group.title}</legend>
            <div className="choice-row">
              {group.options.map(([value, label]) => (
                <button
                  type="button"
                  key={value}
                  aria-pressed={draft[group.field] === value}
                  className={
                    draft[group.field] === value ? "choice selected" : "choice"
                  }
                  onClick={() => setDraft({ ...draft, [group.field]: value })}
                >
                  {draft[group.field] === value && <Check size={13} />}
                  {label}
                </button>
              ))}
            </div>
          </fieldset>
        ))}
        <label className="field-label" htmlFor="output-style">
          What does a great result look like to you?
        </label>
        <textarea
          disabled={saving}
          id="output-style"
          className="style-input"
          maxLength={2000}
          rows={4}
          value={draft.output_style}
          onChange={(event) =>
            setDraft({ ...draft, output_style: event.target.value })
          }
          placeholder="Minimal, thoughtful design. Plain language. Small changes over big rewrites. Whatever feels like you."
        />
        <p className="small muted">
          Your choices guide future batches. Saves and optional pass reasons
          help Skinder learn along the way.
        </p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="primary-button" disabled={saving} type="submit">
          {saving ? "Saving…" : "Save preferences"}
          <ArrowRight size={18} />
        </button>
      </form>
    </Dialog>
  );
}
