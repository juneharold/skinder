export type LoadState = "idle" | "loading" | "ready" | "error";

export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Please try again.";
