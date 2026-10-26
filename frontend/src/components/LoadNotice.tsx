import type { LoadState } from "../lib/async";

export default function LoadNotice({ label, retryLabel = label, state, error, onRetry }: {
  label: string;
  retryLabel?: string;
  state: LoadState;
  error: string;
  onRetry: () => void;
}) {
  if (state !== "error") return null;
  return (
    <div className="error-notice" role="alert">
      <p>Could not load {label}. {error}</p>
      <button onClick={onRetry}>Retry {retryLabel}</button>
    </div>
  );
}
