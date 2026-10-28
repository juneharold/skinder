import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { api, ApiError } from "./lib/api";
import { deferred, installBackend, savedItem, skill } from "./test/fixtures";

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

async function saveFirstCard(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("heading", { name: "Thoughtful Design" });
  await user.click(screen.getByRole("button", { name: /^(Save this skill|Keep saved)$/ }));
  await screen.findByRole("heading", { name: "Clear Writing" });
}

function loseUndoResponse(error = new Error("The response was interrupted.")) {
  const undo = vi.mocked(api.undo).getMockImplementation()!;
  vi.mocked(api.undo).mockImplementationOnce(async (id) => {
    await undo(id);
    throw error;
  });
}

describe("saved collection and undo recovery", () => {
  it("offers a saved-items retry after a failed review invalidates the initial read", async () => {
    const item = skill("research", "Careful Research", "Research");
    installBackend({ saved: [savedItem(item)] });
    const initialRead = deferred<Awaited<ReturnType<typeof api.saved>>>();
    vi.mocked(api.saved).mockReturnValueOnce(initialRead.promise);
    vi.mocked(api.feedback).mockRejectedValueOnce(new Error("Connection unavailable."));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Save this skill" }));
    await screen.findByRole("button", { name: "Retry save" });
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    await user.click(screen.getByRole("link", { name: /Saved/ }));
    await user.click(await screen.findByRole("button", { name: "Retry saved" }));
    expect(await screen.findByRole("heading", { name: item.name })).toBeVisible();

    await act(async () => { initialRead.resolve({ items: [] }); await initialRead.promise; });
    expect(screen.getByRole("heading", { name: item.name })).toBeVisible();
    expect(screen.queryByText("Loading skills…")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    { label: "a lost response", error: new Error("The response was interrupted."), independent: false },
    { label: "a server error", error: new ApiError("Response unavailable.", 503), independent: true },
  ])("recovers undo after $label and preserves independent bookmarks", async ({ error, independent }) => {
    const item = skill("design", "Thoughtful Design");
    const backend = installBackend({ saved: independent ? [savedItem(item)] : [] });
    loseUndoResponse(error);
    const user = userEvent.setup();
    render(<App />);
    await saveFirstCard(user);

    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    expect(await screen.findByRole("heading", { name: item.name })).toBeVisible();
    expect(screen.getByRole("button", { name: independent ? "Keep saved" : "Save this skill" })).toBeEnabled();
    expect(backend.getSession()?.feedback).toEqual([]);
    expect(backend.getSaved().map((entry) => entry.skill.id)).toEqual(independent ? [item.id] : []);
    expect(screen.queryByRole("button", { name: "Retry undo" })).not.toBeInTheDocument();
    expect(backend.getMatchRequests()).toEqual([]);
  });

  it.each(["session", "saved"])("recovers a retry's 404 after the %s reconciliation read failed", async (resource) => {
    const backend = installBackend();
    loseUndoResponse();
    if (resource === "session")
      vi.mocked(api.session).mockRejectedValueOnce(new Error("Could not check the task."));
    const user = userEvent.setup();
    render(<App />);
    await saveFirstCard(user);
    if (resource === "saved")
      vi.mocked(api.saved).mockRejectedValueOnce(new Error("Could not check saved skills."));

    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await screen.findByRole("button", { name: "Retry undo" });
    expect(screen.queryByRole("heading", { name: "Thoughtful Design" })).not.toBeInTheDocument();
    expect(backend.getSession()?.feedback).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Retry undo" }));

    expect(await screen.findByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Save this skill" })).toBeEnabled();
    expect(backend.getSaved()).toEqual([]);
    expect(screen.queryByRole("button", { name: "Retry undo" })).not.toBeInTheDocument();
    expect(backend.getMatchRequests()).toEqual([]);
  });

  it.each(["feedback remains", "session is unavailable"])("does not report undo success when %s", async (condition) => {
    const backend = installBackend();
    vi.mocked(api.undo).mockRejectedValueOnce(new ApiError("Feedback not found.", 404));
    if (condition === "session is unavailable")
      vi.mocked(api.session).mockRejectedValueOnce(new ApiError("Task session not found.", 404));
    const user = userEvent.setup();
    render(<App />);
    await saveFirstCard(user);

    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    expect(await screen.findByRole("button", { name: "Retry undo" })).toBeEnabled();
    expect(screen.getByRole("heading", { name: "Clear Writing" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Thoughtful Design" })).not.toBeInTheDocument();
    expect(backend.getSession()?.feedback.map((entry) => entry.skill_id)).toEqual(["design"]);
    await user.click(screen.getByRole("link", { name: /Saved/ }));
    expect(screen.getByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(screen.queryByText("Last choice undone.")).not.toBeInTheDocument();
  });

  it("waits for the original session before reading Saved and commits recovery together", async () => {
    const backend = installBackend();
    loseUndoResponse();
    const sessionRead = deferred<Awaited<ReturnType<typeof api.session>>>();
    const savedRead = deferred<Awaited<ReturnType<typeof api.saved>>>();
    vi.mocked(api.session).mockReturnValueOnce(sessionRead.promise);
    const user = userEvent.setup();
    render(<App />);
    await saveFirstCard(user);
    const initialSavedReads = vi.mocked(api.saved).mock.calls.length;
    vi.mocked(api.saved).mockReturnValueOnce(savedRead.promise);

    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await waitFor(() => expect(api.session).toHaveBeenCalled());
    expect(api.saved).toHaveBeenCalledTimes(initialSavedReads);
    expect(screen.getByRole("button", { name: "Edit task" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save this skill" })).toBeDisabled();
    await act(async () => { sessionRead.resolve(backend.getSession()!); await sessionRead.promise; });
    await waitFor(() => expect(api.saved).toHaveBeenCalledTimes(initialSavedReads + 1));
    expect(screen.queryByRole("heading", { name: "Thoughtful Design" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Saved\s*1$/ })).toBeVisible();

    await act(async () => { savedRead.resolve({ items: backend.getSaved() }); await savedRead.promise; });
    expect(screen.getByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(screen.getByRole("link", { name: /^Saved\s*0$/ })).toBeVisible();
    expect(screen.getByRole("button", { name: "Edit task" })).toBeEnabled();
  });

  it("does not continue undo reconciliation after signing out", async () => {
    const backend = installBackend();
    loseUndoResponse();
    const sessionRead = deferred<Awaited<ReturnType<typeof api.session>>>();
    vi.mocked(api.session).mockReturnValueOnce(sessionRead.promise);
    const user = userEvent.setup();
    render(<App />);
    await saveFirstCard(user);
    const initialSavedReads = vi.mocked(api.saved).mock.calls.length;

    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await waitFor(() => expect(api.session).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByRole("link", { name: "Continue with GitHub" });
    await act(async () => { sessionRead.resolve(backend.getSession()!); await sessionRead.promise; });

    expect(api.saved).toHaveBeenCalledTimes(initialSavedReads);
    expect(screen.queryByText("Last choice undone.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: /Saved/ }));
    expect(screen.getByText("Sign in to see your collection.")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Thoughtful Design" })).not.toBeInTheDocument();
  });

  it("keeps an undo retry attached to its original task after another task starts", async () => {
    const backend = installBackend();
    loseUndoResponse();
    vi.mocked(api.session).mockRejectedValueOnce(new Error("Could not check the task."));
    const user = userEvent.setup();
    render(<App />);
    await saveFirstCard(user);
    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await screen.findByRole("button", { name: "Retry undo" });
    const original = backend.getSession()!;

    await user.click(screen.getByRole("button", { name: "Edit task" }));
    const task = screen.getByRole("textbox", { name: "What are you working on?" });
    await user.clear(task);
    await user.type(task, "Write clear release notes");
    await user.click(screen.getByRole("button", { name: "Find skills" }));
    await screen.findByRole("heading", { name: "Write clear release notes" });
    vi.mocked(api.session).mockResolvedValueOnce(original);
    await user.click(screen.getByRole("button", { name: "Retry undo" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry undo" })).not.toBeInTheDocument());
    expect(api.session).toHaveBeenLastCalledWith(original.id);
    expect(screen.getByRole("heading", { name: "Write clear release notes" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: original.task })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Saved\s*0$/ })).toBeVisible();
    expect(backend.getMatchRequests()).toHaveLength(1);
  });
});
