import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { api, ApiError, AUTH_EXPIRED_EVENT } from "./lib/api";
import { defaultProfile, type Agent } from "./lib/types";
import { deferred, installBackend, readyHealth, savedItem, sessionFor, skill } from "./test/fixtures";

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());
async function enterTask(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByRole("textbox", { name: "What are you working on?" }), "Review checkout accessibility");
}

describe("hosted matching and accounts", () => {
  it.each<Agent>(["codex", "claude-code"])("matches skills for the selected %s target", async (agent) => {
    const backend = installBackend({ session: null });
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    await user.selectOptions(screen.getByLabelText("Target agent"), agent);
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getMatchRequests()).toEqual([{ task: "Review checkout accessibility", agent }]);
    expect(screen.queryByLabelText("Recommendation engine")).not.toBeInTheDocument();
  });

  it("allows public browsing without loading personal data or submitting paid matches", async () => {
    installBackend({ auth: { user: null, csrf_token: null } });
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByRole("link", { name: "Continue with GitHub" })).toHaveAttribute("href", "/api/auth/github");
    expect(screen.queryByRole("button", { name: "Find skills" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Explore" }));
    expect(await screen.findByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Save Thoughtful Design" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Preferences" })).toBeDisabled();
    await user.click(screen.getByRole("link", { name: /Saved/ }));
    expect(screen.getByRole("heading", { name: "Your saved skills" })).toBeVisible();
    expect(screen.queryByText("No saved skills yet.")).not.toBeInTheDocument();
    expect(api.profile).not.toHaveBeenCalled();
    expect(api.current).not.toHaveBeenCalled();
    expect(api.saved).not.toHaveBeenCalled();
    expect(api.match).not.toHaveBeenCalled();
  });

  it("rechecks hosted availability without asking users to configure a local CLI", async () => {
    installBackend({ session: null, health: { matching_available: false } });
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    expect(screen.queryByText(/codex login|claude auth login/)).not.toBeInTheDocument();
    vi.mocked(api.health).mockResolvedValue(readyHealth);
    await user.click(screen.getByRole("button", { name: "Check availability" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Find skills" })).toBeEnabled());
  });

  it("preserves the selected target through edit, refresh, and continuation", async () => {
    const original = sessionFor([], { agent: "claude-code", status: "cancelled" });
    const backend = installBackend({ session: original });
    const user = userEvent.setup();
    const first = render(<App />);
    await screen.findByRole("heading", { name: original.task });
    first.unmount();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Edit task" }));
    expect(screen.getByLabelText("Target agent")).toHaveValue("claude-code");
    await user.selectOptions(screen.getByLabelText("Target agent"), "codex");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Retry matching" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getMatchRequests()).toEqual([{ task: original.task, agent: "claude-code", session_id: original.id }]);
  });

  it("does not let late preferences replace an edited session's target", async () => {
    installBackend({ session: sessionFor([], { agent: "claude-code" }) });
    const profile = deferred<Awaited<ReturnType<typeof api.profile>>>();
    vi.mocked(api.profile).mockReturnValue(profile.promise);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Edit task" }));
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    await act(async () => { profile.resolve(defaultProfile); await profile.promise; });
    expect(screen.getByLabelText("Target agent")).toHaveValue("claude-code");
  });

  it("reuses an ambiguous submission key even when reconciliation fails", async () => {
    const backend = installBackend({ session: null });
    const accept = vi.mocked(api.match).getMockImplementation()!;
    vi.mocked(api.match).mockImplementationOnce(async (...args) => {
      await accept(...args);
      throw new TypeError("Response lost.");
    });
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    vi.mocked(api.current).mockRejectedValue(new TypeError("Connection unavailable."));
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await user.click(await screen.findByRole("button", { name: "Retry matching" }));
    expect(await screen.findByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    const calls = vi.mocked(api.match).mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[1][2]).toBe(calls[0][2]);
    expect(backend.getMatchRequests()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await user.click(await screen.findByRole("button", { name: "Save this skill" }));
    await user.click(await screen.findByRole("button", { name: "Find more for this task" }));
    await screen.findByRole("heading", { name: "Careful Research" });
    expect(vi.mocked(api.match).mock.calls[2][2]).not.toBe(calls[0][2]);
  });

  it("polls a queued job and cancels that job while preserving previous cards", async () => {
    vi.useFakeTimers();
    const item = skill("design", "Thoughtful Design");
    const queued = sessionFor([item], { status: "queued", latest_job: { id: "new-job", status: "queued", model: "hosted-model" } });
    installBackend({ skills: [item], session: queued });
    await act(async () => { render(<App />); });
    expect(screen.getByRole("heading", { name: "Your task is queued…" })).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(api.session).toHaveBeenCalledWith(queued.id);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop matching" })); });
    expect(api.cancel).toHaveBeenCalledWith("new-job");
    expect(screen.getByRole("heading", { name: item.name })).toBeVisible();
    expect(screen.getByRole("button", { name: "Save this skill" })).toBeEnabled();
  });


  it("keeps a cancellation retry attached to its original job after a new task starts", async () => {
    vi.useFakeTimers();
    const original = sessionFor([], { status: "running" });
    installBackend({ session: original });
    vi.mocked(api.cancel).mockRejectedValueOnce(new Error("Connection unavailable."));
    vi.mocked(api.session).mockResolvedValue(sessionFor([]));
    await act(async () => { render(<App />); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop matching" })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Edit task" })); });
    const next = sessionFor([], { id: "new-session", status: "running",
      latest_job: { id: "new-job", status: "running", model: "hosted-model" } });
    vi.mocked(api.match).mockResolvedValue(next);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Find skills" })); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry cancellation" })); });
    expect(api.cancel).toHaveBeenLastCalledWith(original.latest_job.id);
    expect(screen.getByRole("button", { name: "Stop matching" })).toBeEnabled();
    expect(screen.queryByText("Matching stopped.")).not.toBeInTheDocument();
  });

  it("clears private data on logout and rejects an earlier poll", async () => {
    const item = skill("design", "Thoughtful Design");
    const running = sessionFor([item], { status: "running" });
    installBackend({ session: running, saved: [savedItem(item)] });
    const poll = deferred<Awaited<ReturnType<typeof api.session>>>();
    vi.mocked(api.session).mockReturnValue(poll.promise);
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText("@alice")).toBeVisible();
    await waitFor(() => expect(api.session).toHaveBeenCalled(), { timeout: 2000 });
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByRole("link", { name: "Continue with GitHub" });
    await act(async () => { poll.resolve(running); await poll.promise; });
    expect(screen.queryByRole("heading", { name: running.task })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop matching" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: /Saved/ }));
    expect(screen.getByText("Sign in to see your collection.")).toBeVisible();
    expect(screen.queryByRole("heading", { name: item.name })).not.toBeInTheDocument();
  });

  it("clears personal state when authentication expires", async () => {
    installBackend({ saved: [savedItem(skill("design", "Thoughtful Design"))] });
    render(<App />);
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    act(() => window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT)));
    expect(screen.getByRole("link", { name: "Continue with GitHub" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Thoughtful Design" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preferences" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("session expired");
  });

  it("retains an original target when retrying after draft edits", async () => {
    const backend = installBackend({ session: null });
    vi.mocked(api.match).mockRejectedValueOnce(new ApiError("Please retry.", 503));
    const user = userEvent.setup();
    render(<App />);
    await enterTask(user);
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("button", { name: "Retry matching" });
    await user.selectOptions(screen.getByLabelText("Target agent"), "claude-code");
    await user.click(within(screen.getByRole("alert")).getByRole("button", { name: "Retry matching" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()?.agent).toBe("codex");
  });
});
