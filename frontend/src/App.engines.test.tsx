import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { api, ApiError } from "./lib/api";
import { defaultProfile, type Agent, type Engine } from "./lib/types";
import { deferred, installBackend, readyEngines, sessionFor, skill } from "./test/fixtures";

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());
const unavailable = { installed: false, authenticated: false, ready: false, message: "Install the CLI." };
const onlyClaude = { ...readyEngines, codex: unavailable };

async function enterTask(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByRole("textbox", { name: "What are you working on?" }), "Review checkout accessibility");
}

describe("recommendation engines", () => {
  it.each<[Agent, Engine]>([
    ["codex", "codex"], ["codex", "claude-code"],
    ["claude-code", "codex"], ["claude-code", "claude-code"],
  ])("matches %s skills using %s independently", async (agent, engine) => {
    const backend = installBackend({ session: null });
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    expect(screen.getByLabelText("Recommendation engine")).toHaveValue("codex");
    await user.selectOptions(screen.getByLabelText("Target agent"), agent);
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), engine);
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()).toMatchObject({ task: "Review checkout accessibility", agent, engine });
    expect(backend.getMatchRequests()).toEqual([{ task: "Review checkout accessibility", agent, engine }]);
  });

  it("allows selecting the only ready engine even when Codex is missing", async () => {
    const backend = installBackend({ session: null, engines: onlyClaude });
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    expect(screen.getByLabelText("Recommendation engine")).toBeEnabled();
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "claude-code");
    expect(screen.getByText("Claude Code connected")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()).toMatchObject({ agent: "codex", engine: "claude-code" });
  });

  it("shows Claude setup and blocks matching only while that engine is unavailable", async () => {
    installBackend({ session: null, engines: { ...readyEngines, "claude-code": unavailable } });
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "claude-code");
    expect(screen.getByText("claude auth login")).toBeVisible();
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "codex");
    expect(screen.getByRole("button", { name: "Find skills" })).toBeEnabled();
  });

  it("shows the selected engine's upgrade guidance when its installed CLI is unsupported", async () => {
    const upgradeMessage = "Upgrade Claude Code to support structured recommendations.";
    installBackend({ session: null, engines: {
      ...readyEngines,
      "claude-code": { installed: true, authenticated: true, ready: false, message: upgradeMessage },
    } });
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    expect(screen.queryByText(upgradeMessage)).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "claude-code");
    expect(screen.getByText(upgradeMessage)).toBeVisible();
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "codex");
    expect(screen.queryByText(upgradeMessage)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find skills" })).toBeEnabled();
  });

  it("restores a session's engine on refresh and edit, and changing it starts a new deck", async () => {
    const original = sessionFor([], { agent: "claude-code", engine: "claude-code" });
    const backend = installBackend({ session: original });
    const user = userEvent.setup();
    const first = render(<App />);
    await screen.findByRole("heading", { name: original.task });
    first.unmount();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Edit task" }));
    expect(screen.getByLabelText("Target agent")).toHaveValue("claude-code");
    expect(screen.getByLabelText("Recommendation engine")).toHaveValue("claude-code");
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "codex");
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()).toMatchObject({ task: original.task, agent: "claude-code", engine: "codex" });
    expect(backend.getSession()?.id).not.toBe(original.id);
    expect(backend.getMatchRequests()[0]).not.toHaveProperty("session_id");
    const sidebar = screen.getByText("Recommendation engine").closest("aside")!;
    expect(within(sidebar).getByText("Codex")).toBeVisible();
  });

  it.each(["failed", "cancelled"] as const)("retries a %s session with its saved engine when only Claude is ready", async (status) => {
    const original = sessionFor([], { engine: "claude-code", status });
    const backend = installBackend({ session: original, engines: onlyClaude });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Retry matching" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getMatchRequests()).toEqual([
      { task: original.task, agent: original.agent, engine: "claude-code", session_id: original.id },
    ]);
  });

  it("uses the saved engine for another batch after cancelled draft edits", async () => {
    const original = sessionFor([skill("design", "Thoughtful Design")], { engine: "claude-code" });
    const backend = installBackend({ session: original, engines: onlyClaude });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Edit task" }));
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "codex");
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await user.click(await screen.findByRole("button", { name: "Find more for this task" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    expect(backend.getMatchRequests()).toEqual([
      { task: original.task, agent: original.agent, engine: "claude-code", session_id: original.id },
    ]);
  });

  it("keeps a session retry disabled when only the other engine is ready", async () => {
    installBackend({ session: sessionFor([], { status: "failed" }), engines: onlyClaude });
    render(<App />);
    expect(await screen.findByRole("button", { name: "Retry matching" })).toBeDisabled();
    expect(api.match).not.toHaveBeenCalled();
  });

  it("retries the stored engine independently of the current draft selection", async () => {
    const backend = installBackend({ session: null, engines: onlyClaude });
    vi.mocked(api.match).mockRejectedValueOnce(new ApiError("Please retry.", 503));
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "claude-code");
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("button", { name: "Retry matching" });
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "codex");
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Retry matching" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Retry matching" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()?.engine).toBe("claude-code");
  });

  it("disables a stored retry when its engine becomes unavailable even if the draft engine is ready", async () => {
    installBackend({ session: null, engines: { ...readyEngines, "claude-code": unavailable } });
    vi.mocked(api.match).mockRejectedValueOnce(new ApiError("Please retry.", 503));
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("button", { name: "Retry matching" });
    await user.selectOptions(screen.getByLabelText("Recommendation engine"), "claude-code");
    vi.mocked(api.health).mockResolvedValue({ status: "ok", catalog_count: 3, indexed_count: 3, retrieval_mode: "hybrid", engines: onlyClaude });
    await user.click(screen.getByRole("button", { name: "Recheck connection" }));
    await screen.findByText("Claude Code connected");
    expect(screen.getByRole("button", { name: "Find skills" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Retry matching" })).toBeDisabled();
    expect(api.match).toHaveBeenCalledTimes(1);
  });

  it("does not let a late profile response replace an edited session's target or engine", async () => {
    installBackend({ session: sessionFor([], { agent: "claude-code", engine: "claude-code" }) });
    const profile = deferred<Awaited<ReturnType<typeof api.profile>>>();
    vi.mocked(api.profile).mockReturnValue(profile.promise);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Edit task" }));
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    await act(async () => { profile.resolve(defaultProfile); await profile.promise; });
    expect(screen.getByLabelText("Target agent")).toHaveValue("claude-code");
    expect(screen.getByLabelText("Recommendation engine")).toHaveValue("claude-code");
    expect(screen.getByRole("button", { name: "Find skills" })).toBeEnabled();
  });

  it("waits for the current session before allowing a new request with a ready engine", async () => {
    installBackend({ session: null, engines: onlyClaude });
    const current = deferred<Awaited<ReturnType<typeof api.current>>>();
    vi.mocked(api.current).mockReturnValue(current.promise);
    render(<App />);
    await screen.findByText("Loading your task…");
    expect(screen.queryByRole("button", { name: "Find skills" })).not.toBeInTheDocument();
    expect(api.match).not.toHaveBeenCalled();
    await act(async () => { current.resolve(null); await current.promise; });
    expect(screen.getByLabelText("Recommendation engine")).toBeEnabled();
  });

  it("does not reconcile a lost new-match response to another engine's session", async () => {
    const backend = installBackend({ session: null });
    vi.mocked(api.match).mockRejectedValueOnce(new TypeError("Response lost."));
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    vi.mocked(api.current).mockResolvedValue(sessionFor(backend.skills, {
      id: "another-engine", task: "Review checkout accessibility", engine: "claude-code",
    }));
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    expect(await screen.findByRole("button", { name: "Retry matching" })).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "Thoughtful Design" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry matching" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()?.engine).toBe("codex");
    expect(api.match).toHaveBeenCalledTimes(2);
  });

  it("does not reconcile a lost continuation response to an engine mismatch", async () => {
    const original = sessionFor([], { engine: "claude-code", status: "cancelled" });
    const backend = installBackend({ session: original });
    vi.mocked(api.match).mockRejectedValueOnce(new TypeError("Response lost."));
    vi.mocked(api.session).mockResolvedValue(sessionFor(backend.skills, {
      ...original, engine: "codex", status: "completed", updated_at: "2026-10-27T03:00:00Z",
    }));
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Retry matching" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Response lost."));
    expect(screen.getByRole("heading", { name: "Matching cancelled." })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Thoughtful Design" })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("alert")).getByRole("button", { name: "Retry matching" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()?.engine).toBe("claude-code");
    expect(api.match).toHaveBeenCalledTimes(2);
  });
});
