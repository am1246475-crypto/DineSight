import { ApiError } from "../api/client";
import { config } from "../config";

/** A short, human explanation for a failed request. */
export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return `Cannot reach the backend at ${config.apiUrl}. Is it running?`;
    if (e.status === 404) return `The backend has no ${e.path || "such"} endpoint yet (404).`;
    if (e.status >= 500) return `The backend failed while handling ${e.path || "the request"} (${e.message}).`;
    return `${e.path || "Request"} failed: ${e.message}.`;
  }
  return e instanceof Error ? e.message : "Something went wrong.";
}

export function ErrorBox({ title, error, onRetry, busy }: { title: string; error: unknown; onRetry?: () => void; busy?: boolean }) {
  return (
    <div className="err-box" role="alert">
      <b>{title}</b>
      <span>{describeError(error)}</span>
      {onRetry && <button className="ghost" onClick={onRetry} disabled={busy}>{busy ? "Retrying…" : "Try again"}</button>}
    </div>
  );
}

/** Shimmering placeholder. `ratio` keeps the layout from jumping when the real content arrives. */
export const SkeletonBox = ({ ratio, height, label }: { ratio?: string; height?: number; label: string }) => (
  <div className="skel-box" style={{ aspectRatio: ratio, height }} role="status" aria-label={label} />
);
