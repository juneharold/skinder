import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import App from "./App";
import { api } from "./lib/api";
import { deferred, installBackend, savedItem, sessionFor, skill } from "./test/fixtures";

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("skill discovery", () => {
  it("retries a failed save for the same skill without starting a new match", async () => {
    const backend = installBackend();
    vi.mocked(api.feedback).mockRejectedValueOnce(
      new Error("The local database is unavailable."),
    );
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("button", { name: "Retry save" });
    expect(screen.getByRole("heading", { name: "Thoughtful Design" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Retry save" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    expect(api.feedback).toHaveBeenNthCalledWith(1, "session-1", "design", "save", null);
    expect(api.feedback).toHaveBeenNthCalledWith(2, "session-1", "design", "save", null);
    expect(backend.getSession()?.feedback.map((item) => item.skill_id)).toEqual(["design"]);
    expect(backend.getSaved().map((item) => item.skill.id)).toEqual(["design"]);
    expect(backend.getMatchRequests()).toEqual([]);
  });

  it("retries a failed pass for the same skill without starting a new match", async () => {
    const backend = installBackend();
    vi.mocked(api.feedback).mockRejectedValueOnce(new Error("Please try again."));
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Pass with a reason" }));
    await user.click(screen.getByRole("button", { name: "Not my style" }));
    await user.click(await screen.findByRole("button", { name: "Retry pass" }));
    await screen.findByRole("heading", { name: "Clear Writing" });

    expect(api.feedback).toHaveBeenNthCalledWith(1, "session-1", "design", "pass", "wrong-style");
    expect(api.feedback).toHaveBeenNthCalledWith(2, "session-1", "design", "pass", "wrong-style");
    expect(backend.getSession()?.feedback).toMatchObject([
      { skill_id: "design", action: "pass", reason: "wrong-style" },
    ]);
    expect(backend.getSaved()).toEqual([]);
    expect(backend.getMatchRequests()).toEqual([]);
  });

  it("uses mutation responses to save and undo without re-reading saved skills", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    const initialSavedReads = vi.mocked(api.saved).mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    expect(backend.getSaved().map((item) => item.skill.id)).toEqual(["design"]);
    expect(vi.mocked(api.saved).mock.calls).toHaveLength(initialSavedReads);

    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession()?.feedback).toEqual([]);
    expect(backend.getSaved()).toEqual([]);
    expect(vi.mocked(api.saved).mock.calls).toHaveLength(initialSavedReads);
  });

  it("keeps an Explore bookmark after undoing a separate deck save", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("link", { name: "Explore" }));
    await user.click(screen.getByRole("button", { name: "Save Thoughtful Design" }));
    await screen.findByRole("button", { name: "Remove Thoughtful Design from saved" });
    await user.click(screen.getByRole("link", { name: "Discover" }));

    await user.click(await screen.findByRole("button", { name: "Keep saved" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });

    expect(backend.getSession()?.feedback).toEqual([]);
    expect(backend.getSaved().map((item) => item.skill.id)).toEqual(["design"]);
    expect(screen.getByRole("button", { name: "Keep saved" })).toBeVisible();
  });

  it("retries a lost direct-bookmark response idempotently without changing the deck", async () => {
    const backend = installBackend();
    const save = vi.mocked(api.save).getMockImplementation()!;
    vi.mocked(api.save).mockImplementationOnce(async (id) => {
      await save(id);
      throw new Error("The response was interrupted.");
    });
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    const initialSavedReads = vi.mocked(api.saved).mock.calls.length;
    await user.click(screen.getByRole("link", { name: "Explore" }));
    await user.click(screen.getByRole("button", { name: "Save Thoughtful Design" }));
    expect(await screen.findByRole("button", { name: "Retry save" })).toBeVisible();
    expect(backend.getSaved().map((item) => item.skill.id)).toEqual(["design"]);
    await user.click(screen.getByRole("button", { name: "Retry save" }));

    expect(backend.getSaved().map((item) => item.skill.id)).toEqual(["design"]);
    expect(backend.getSession()?.feedback).toEqual([]);
    expect(api.feedback).not.toHaveBeenCalled();
    expect(backend.getMatchRequests()).toEqual([]);
    expect(api.save).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.saved).mock.calls).toHaveLength(initialSavedReads);

    await user.click(screen.getByRole("link", { name: "Discover" }));
    expect(screen.getByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(backend.getSession()?.feedback).toEqual([]);
  });

  it("keeps a saved item visible when removing its bookmark fails", async () => {
    const saved = savedItem(skill("design", "Thoughtful Design"));
    installBackend({ session: null, saved: [saved] });
    vi.mocked(api.unsave).mockRejectedValueOnce(new Error("Could not reach Skinder."));
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Deal me in." });
    await user.click(screen.getByRole("link", { name: /^Saved/ }));
    await user.click(
      screen.getByRole("button", { name: "Remove Thoughtful Design from saved" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach Skinder");
    expect(screen.getByRole("button", { name: "Remove Thoughtful Design from saved" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry removal" })).toBeVisible();
  });

  it("keeps catalog browsing available when another boot resource fails", async () => {
    installBackend();
    vi.mocked(api.profile).mockRejectedValueOnce(new Error("Profile is unavailable."));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("link", { name: "Explore" }));
    expect(await screen.findByText("A practical approach to thoughtful design.")).toBeVisible();
  });

  it("restores catalog filters and view through browser history", async () => {
    window.history.replaceState({}, "", "/?view=explore&q=Clear&category=Writing");
    installBackend();
    const user = userEvent.setup();
    render(<App />);

    const search = await screen.findByRole("textbox", { name: "Search skills" });
    expect(search).toHaveValue("Clear");
    expect(screen.getByLabelText("Category")).toHaveValue("Writing");
    expect(screen.getByRole("heading", { name: "Clear Writing" })).toBeVisible();

    await user.click(screen.getByRole("link", { name: /^Saved/ }));
    await screen.findByRole("heading", { name: "A hand worth keeping." });
    await act(async () => window.history.back());
    await screen.findByRole("heading", { name: "Clear Writing" });
    expect(screen.getByRole("link", { name: "Explore" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("textbox", { name: "Search skills" })).toHaveValue("Clear");
    expect(screen.getByLabelText("Category")).toHaveValue("Writing");
  });

  it("keeps task edits separate until matching starts, then reuses the active context for more", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Edit task" }));
    const editDialog = screen.getByRole("dialog", { name: "Edit task" });
    const taskInput = within(editDialog).getByRole("textbox", { name: /What are you working on/ });
    await user.clear(taskInput);
    await user.type(taskInput, "Review interviews for checkout problems");
    await user.click(within(editDialog).getByRole("button", { name: "Cancel" }));
    expect(backend.getSession()?.task).toBe("Build a thoughtful landing page");
    expect(api.match).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Edit task" }));
    const secondDialog = screen.getByRole("dialog", { name: "Edit task" });
    const secondTask = within(secondDialog).getByRole("textbox", { name: /What are you working on/ });
    await user.clear(secondTask);
    await user.type(secondTask, "Review interviews for checkout problems");
    await user.click(within(secondDialog).getByRole("button", { name: "Find skills" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(api.match).toHaveBeenCalledWith(
      "Review interviews for checkout problems",
      "codex",
      undefined,
    );
    expect(backend.getSession()?.task).toBe("Review interviews for checkout problems");

    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Careful Research" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Batch reviewed." });
    await user.click(screen.getByRole("button", { name: "Find more for this task" }));
    await screen.findByRole("button", { name: "Keep saved" });
    expect(api.match).toHaveBeenLastCalledWith(
      "Review interviews for checkout problems",
      "codex",
      "session-2",
    );
  });

  it("locks task submission while a match request is pending", async () => {
    const backend = installBackend({ session: null });
    const match = deferred<Awaited<ReturnType<typeof api.match>>>();
    vi.mocked(api.match).mockReturnValue(match.promise);
    const user = userEvent.setup();
    render(<App />);

    const taskInput = await screen.findByRole("textbox", { name: /What are you working on/ });
    await user.type(taskInput, "Plan an onboarding flow");
    const submit = screen.getByRole("button", { name: "Find skills" });
    await user.click(submit);
    await user.click(submit);
    expect(api.match).toHaveBeenCalledTimes(1);
    expect(submit).toBeDisabled();

    await act(async () => {
      match.resolve(
        sessionFor(backend.skills, { id: "session-2", task: "Plan an onboarding flow" }),
      );
      await match.promise;
    });
    await screen.findByRole("heading", { name: "Thoughtful Design" });
  });

  it("does not let an older Strict Mode session read undo a completed card action", async () => {
    const backend = installBackend();
    const staleRead = deferred<Awaited<ReturnType<typeof api.current>>>();
    const currentRead = deferred<Awaited<ReturnType<typeof api.current>>>();
    vi.mocked(api.current)
      .mockReturnValueOnce(staleRead.promise)
      .mockReturnValueOnce(currentRead.promise);
    const user = userEvent.setup();
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );

    await act(async () => {
      currentRead.resolve(sessionFor(backend.skills));
      await currentRead.promise;
    });
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Clear Writing" });

    await act(async () => {
      staleRead.resolve(sessionFor(backend.skills));
      await staleRead.promise;
    });
    expect(screen.getByRole("heading", { name: "Clear Writing" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Undo last choice" })).toBeEnabled();
    expect(backend.getSession()?.feedback.map((item) => item.skill_id)).toEqual(["design"]);
  });

  it("ignores an old saved-items failure after a newer bookmark succeeds", async () => {
    const backend = installBackend();
    const staleRead = deferred<Awaited<ReturnType<typeof api.saved>>>();
    const currentRead = deferred<Awaited<ReturnType<typeof api.saved>>>();
    vi.mocked(api.saved)
      .mockReturnValueOnce(staleRead.promise)
      .mockReturnValueOnce(currentRead.promise);
    const user = userEvent.setup();
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );

    await act(async () => {
      currentRead.resolve({ items: [] });
      await currentRead.promise;
    });
    await user.click(await screen.findByRole("link", { name: "Explore" }));
    await user.click(screen.getByRole("button", { name: "Save Thoughtful Design" }));
    const remove = await screen.findByRole("button", {
      name: "Remove Thoughtful Design from saved",
    });

    await act(async () => {
      staleRead.reject(new Error("An old saved-items read failed."));
      await staleRead.promise.catch(() => undefined);
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(remove).toBeEnabled();
    expect(backend.getSaved().map((item) => item.skill.id)).toEqual(["design"]);
  });

  it("keeps a bookmark failure with the skill that failed", async () => {
    installBackend({ saved: [savedItem(skill("design", "Thoughtful Design"))] });
    vi.mocked(api.unsave).mockRejectedValueOnce(new Error("Removal failed."));
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("link", { name: /^Saved/ }));
    await user.click(
      screen.getByRole("button", { name: "Remove Thoughtful Design from saved" }),
    );
    await screen.findByRole("button", { name: "Retry removal" });
    await user.click(screen.getByRole("link", { name: "Explore" }));
    await user.click(await screen.findByRole("button", { name: "Clear Writing" }));

    const details = screen.getByRole("dialog", { name: "Skill details" });
    expect(within(details).queryByRole("button", { name: "Retry removal" })).not.toBeInTheDocument();
  });

  it("does not treat arrow keys in the task field as deck actions", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Edit task" }));
    const dialog = screen.getByRole("dialog", { name: "Edit task" });
    await user.click(within(dialog).getByRole("textbox", { name: /What are you working on/ }));
    await user.keyboard("{ArrowLeft}{ArrowRight}");
    expect(backend.getSession()?.feedback).toEqual([]);
    expect(screen.getByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
  });

  it("keeps preference drafts after a failed save so they can be retried", async () => {
    const backend = installBackend();
    vi.mocked(api.saveProfile).mockRejectedValueOnce(
      new Error("Preferences could not be saved."),
    );
    const user = userEvent.setup();
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );

    await user.click(await screen.findByRole("button", { name: "Preferences" }));
    const dialog = screen.getByRole("dialog", { name: "Preferences" });
    await user.click(within(dialog).getByRole("button", { name: "Think it through" }));
    const output = within(dialog).getByRole("textbox", {
      name: "What does a great result look like to you?",
    });
    await user.type(output, "Warm, minimal visuals.");
    await user.click(within(dialog).getByRole("button", { name: "Save preferences" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Preferences could not be saved",
    );
    expect(output).toHaveValue("Warm, minimal visuals.");
    expect(within(dialog).getByRole("button", { name: "Think it through" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(within(dialog).getByRole("button", { name: "Save preferences" }));
    await waitForDialogToClose();
    expect(api.saveProfile).toHaveBeenLastCalledWith(
      expect.objectContaining({ planning: "plan-first", output_style: "Warm, minimal visuals." }),
    );
    expect(backend.getProfile()).toMatchObject({ planning: "plan-first", output_style: "Warm, minimal visuals." });
  });

  it("locks preference reopening during a save and retries the original draft after close", async () => {
    const backend = installBackend();
    const request = deferred<Awaited<ReturnType<typeof api.saveProfile>>>();
    vi.mocked(api.saveProfile).mockReturnValueOnce(request.promise);
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Preferences" }));
    const dialog = screen.getByRole("dialog", { name: "Preferences" });
    await user.click(within(dialog).getByRole("button", { name: "Think it through" }));
    const output = within(dialog).getByRole("textbox", {
      name: "What does a great result look like to you?",
    });
    await user.type(output, "Warm, minimal visuals.");
    await user.click(within(dialog).getByRole("button", { name: "Save preferences" }));
    expect(
      within(dialog).getByRole("button", { name: /Saving/ }),
    ).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Close dialog" }));
    expect(screen.queryByRole("dialog", { name: "Preferences" })).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("banner")).getByRole("button", { name: "Preferences" }),
    ).toBeDisabled();

    await act(async () => {
      request.reject(new Error("The save response was lost."));
      await request.promise.catch(() => undefined);
    });
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("button", { name: "Retry preferences" })).toBeEnabled();
    await user.click(within(alert).getByRole("button", { name: "Retry preferences" }));
    await waitForDialogToClose();
    expect(api.saveProfile).toHaveBeenLastCalledWith(
      expect.objectContaining({ planning: "plan-first", output_style: "Warm, minimal visuals." }),
    );
    expect(backend.getProfile()).toMatchObject({ planning: "plan-first", output_style: "Warm, minimal visuals." });

    await user.click(
      within(screen.getByRole("banner")).getByRole("button", { name: "Preferences" }),
    );
    const reopened = screen.getByRole("dialog", { name: "Preferences" });
    expect(within(reopened).getByRole("button", { name: "Think it through" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(reopened).getByRole("textbox", {
      name: "What does a great result look like to you?",
    })).toHaveValue("Warm, minimal visuals.");
  });

  it("keeps source and install instructions available when copying fails", async () => {
    installBackend();
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("Clipboard unavailable.")) },
    });
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "View skill & source" }));
    const dialog = screen.getByRole("dialog", { name: "Skill details" });
    await user.click(within(dialog).getByRole("button", { name: "Copy installation command" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent(
      "Copy is unavailable in this browser",
    );
    expect(within(dialog).getByText(/npx skills add example\/skills/)).toBeVisible();
    expect(within(dialog).getByRole("link", { name: "Read the skill" })).toHaveAttribute(
      "href",
      "https://github.com/example/skills/blob/abc/design/SKILL.md",
    );
  });
});

async function waitForDialogToClose() {
  await vi.waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
}
