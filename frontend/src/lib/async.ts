export type LoadState = "loading" | "ready" | "error";

export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Please try again.";
