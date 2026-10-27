import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import App from "./App";
import { api } from "./lib/api";
import { deferred, installBackend, readyHealth, savedItem, sessionFor, skill } from "./test/fixtures";

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("skill discovery", () => {
  it("uses server totals and categories while loading one catalog page at a time", async () => {
    window.history.replaceState({}, "", "/?view=explore");
    const skills = Array.from({ length: 51 }, (_, index) =>
      skill(`skill-${index}`, `Skill ${index}`, index === 50 ? "Writing" : "Design"),
    );
    installBackend({ session: null, skills });
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByText("51 skills")).toBeVisible();
    expect(screen.getByRole("option", { name: "Writing" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Skill 50" })).not.toBeInTheDocument();
    expect(api.catalog).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("heading", { name: "Skill 50" })).toBeVisible();
    expect(api.catalog).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 50, limit: 50 }), expect.any(AbortSignal),
    );
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(51);
    await user.selectOptions(screen.getByLabelText("Category"), "Writing");
    expect(await screen.findByText("1 skill in Writing")).toBeVisible();
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(api.catalog).toHaveBeenLastCalledWith(
      expect.objectContaining({ category: "Writing", offset: 0 }), expect.any(AbortSignal),
    );
  });

  it("can find another batch when the reviewed catalog page has no candidates left", async () => {
    const skills = Array.from({ length: 51 }, (_, index) => skill(`skill-${index}`, `Skill ${index}`));
    const feedback = skills.slice(0, 50).map((item, index) => ({
      id: `choice-${index}`, session_id: "session-1", skill_id: item.id,
      action: "pass" as const, reason: null, created_at: "2026-10-27T00:00:00Z",
    }));
    installBackend({ skills, session: sessionFor([skills[0]], { feedback }) });
    vi.mocked(api.match).mockResolvedValue(sessionFor([skills[50]], { feedback }));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Find more for this task" }));
    expect(await screen.findByRole("heading", { name: "Skill 50" })).toBeVisible();
    expect(api.skill).toHaveBeenCalledWith("skill-50");
    expect(screen.queryByRole("heading", { name: "All available skills reviewed." })).not.toBeInTheDocument();
  });

  it("ignores an old next page after the search changes, even if it ignores cancellation", async () => {
    window.history.replaceState({}, "", "/?view=explore");
    const first = skill("alpha", "Alpha Skill");
    const match = skill("beta", "Beta Skill", "Writing");
    const stale = skill("stale", "Stale Skill");
    installBackend({ session: null, skills: [first, match, stale] });
    const nextPage = deferred<Awaited<ReturnType<typeof api.catalog>>>();
    vi.mocked(api.catalog).mockImplementation(async (filters = {}) => {
      if (filters.offset) return nextPage.promise;
      return {
        skills: filters.q === "Beta" ? [match] : [first],
        categories: ["Design", "Writing"],
        total: filters.q === "Beta" ? 1 : 3,
        has_more: !filters.q,
      };
    });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Load more" }));
    await user.type(screen.getByRole("textbox", { name: "Search skills" }), "Beta");
    expect(await screen.findByRole("heading", { name: "Beta Skill" })).toBeVisible();
    await act(async () => {
      nextPage.resolve({ skills: [stale], categories: ["Design"], total: 3, has_more: false });
      await nextPage.promise;
    });
    expect(screen.queryByRole("heading", { name: "Stale Skill" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Alpha Skill" })).not.toBeInTheDocument();
    expect(screen.getByText("1 skill matching “Beta”")).toBeVisible();
    expect(api.catalog).toHaveBeenCalledTimes(3);
  });

  it("hydrates a restored recommendation outside the catalog page and keeps it through filtering", async () => {
    const match = skill("match", "Restored Match");
    const visible = skill("visible", "Visible Skill", "Writing");
    installBackend({ skills: [match, visible], session: sessionFor([match]) });
    vi.mocked(api.catalog).mockResolvedValue({ skills: [visible], categories: ["Writing"], total: 1, has_more: false });
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Restored Match" })).toBeVisible();
    expect(api.skill).toHaveBeenCalledWith("match");
    await user.click(screen.getByRole("link", { name: "Explore" }));
    await user.type(screen.getByRole("textbox", { name: "Search skills" }), "Visible");
    await waitFor(() => expect(api.catalog).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "Visible", offset: 0 }), expect.any(AbortSignal),
    ));
    await user.click(screen.getByRole("link", { name: "Discover" }));
    expect(screen.getByRole("heading", { name: "Restored Match" })).toBeVisible();
    expect(api.skill).toHaveBeenCalledTimes(1);
  });

  it("offers a retry for failed recommendation details without claiming the deck is empty", async () => {
    installBackend();
    vi.mocked(api.catalog).mockRejectedValue(new Error("The catalog is unavailable."));
    vi.mocked(api.skill).mockRejectedValueOnce(new Error("The skill is unavailable."));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Retry skills" }));
    expect(screen.queryByRole("heading", { name: "No matching skills yet." })).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(api.match).not.toHaveBeenCalled();
  });

  it("keeps unknown imported metadata explicit and directs setup to the source", async () => {
    const imported = {
      ...skill("imported", "Imported Skill"),
      provenance: "skillsmp" as const,
      compatibility: null,
      revision: null,
      license: null,
      workflow: [],
      requirements: [],
      output_style: [],
      evidence: [],
      provider_url: "https://skillsmp.com/skills/imported",
    };
    installBackend({ skills: [imported] });
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByText("Compatibility not verified")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "View skill & source" }));
    const dialog = screen.getByRole("dialog", { name: "Skill details" });
    expect(within(dialog).getByText(/License unknown.*Source revision unknown/)).toBeVisible();
    expect(within(dialog).getByText(/workflow has not been verified/)).toBeVisible();
    expect(within(dialog).getByText(/Setup requirements have not been verified/)).toBeVisible();
    expect(within(dialog).getByText(/No reviewed working-style traits/)).toBeVisible();
    expect(within(dialog).getByRole("link", { name: "View on SkillsMP" })).toHaveAttribute("href", imported.provider_url);
    expect(within(dialog).queryByRole("button", { name: "Copy installation command" })).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/npx skills/)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Claude Code" })).not.toBeInTheDocument();
  });

  it("explains when search is limited to keywords", async () => {
    installBackend();
    vi.mocked(api.health).mockResolvedValue({
      status: "ok", catalog_count: 3, indexed_count: 0, retrieval_mode: "keyword-only",
      model: readyHealth.model, matching_available: true,
    });
    render(<App />);
    expect(await screen.findByText(/Search is using keywords only/)).toBeVisible();
  });

  it("keeps catalog bookmarks usable when matching is unavailable", async () => {
    const backend = installBackend({ session: null });
    vi.mocked(api.health).mockResolvedValue({
      status: "ok", catalog_count: backend.skills.length,
      indexed_count: backend.skills.length, retrieval_mode: "hybrid",
      model: readyHealth.model, matching_available: false,
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByRole("textbox", { name: "What are you working on?" }), "Build a portfolio");
    expect(screen.getByRole("button", { name: "Find skills" })).toBeDisabled();
    await user.click(screen.getByRole("link", { name: "Explore" }));
    await user.click(await screen.findByRole("button", { name: "Save Thoughtful Design" }));
    await screen.findByRole("button", { name: "Remove Thoughtful Design from saved" });
    expect(backend.getSaved().map(item => item.skill.id)).toEqual(["design"]);
    expect(backend.getSession()).toBeNull();
    expect(backend.getMatchRequests()).toEqual([]);
  });

  it("retries a lost save response idempotently without starting a new match", async () => {
    const backend = installBackend();
    const feedback = vi.mocked(api.feedback).getMockImplementation()!;
    vi.mocked(api.feedback).mockImplementationOnce(async (...args) => {
      await feedback(...args);
      throw new Error("The response was interrupted.");
    });
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

  it("does not restore a deck bookmark removed from Saved before undo", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    await user.click(screen.getByRole("link", { name: /^Saved/ }));
    await user.click(
      screen.getByRole("button", { name: "Remove Thoughtful Design from saved" }),
    );
    expect(backend.getSaved()).toEqual([]);

    await user.click(screen.getByRole("link", { name: "Discover" }));
    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });

    expect(backend.getSession()?.feedback).toEqual([]);
    expect(backend.getSaved()).toEqual([]);
    expect(screen.getByRole("button", { name: "Save this skill" })).toBeVisible();
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

    await screen.findByRole("textbox", { name: "What are you working on?" });
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
    expect(screen.getByRole("link", { name: /^Saved/ })).toHaveAttribute("aria-current", "page");
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
      expect.any(String),
      undefined,
    );
    expect(backend.getSession()?.task).toBe("Review interviews for checkout problems");

    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Batch reviewed." });
    await user.click(screen.getByRole("button", { name: "Find more for this task" }));
    await screen.findByRole("heading", { name: "Careful Research" });
    expect(api.match).toHaveBeenLastCalledWith(
      "Review interviews for checkout problems",
      "codex",
      expect.any(String),
      "session-2",
    );
  });

  it("continues card review with the keyboard after a save", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save this skill" })).toBeEnabled(),
    );
    const deck = screen.getByRole("region", { name: "Skill deck" });
    expect(deck).toHaveFocus();
    fireEvent.keyDown(deck, { key: "ArrowRight" });
    expect(api.feedback).toHaveBeenCalledTimes(2);

    await screen.findByRole("heading", { name: "Careful Research" });
    expect(backend.getSession()?.feedback.map((item) => item.skill_id)).toEqual([
      "design",
      "writing",
    ]);
  });

  it("offers a new task after a no-fit result without starting another match", async () => {
    const backend = installBackend({ session: sessionFor([], { status: "completed" }) });
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "No matching skills yet." });
    expect(screen.getByRole("button", { name: "Change task" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Change task" }));
    expect(screen.getByRole("dialog", { name: "Edit task" })).toBeVisible();
    expect(backend.getMatchRequests()).toEqual([]);
  });

  it.each(["failed", "cancelled"] as const)("retries a %s task even when all previous cards were reviewed", async (status) => {
    const item = skill("design", "Thoughtful Design");
    const session = sessionFor([item], {
      status,
      feedback: [{
        id: "choice-1", session_id: "session-1", skill_id: item.id,
        action: "pass", reason: null, created_at: "2026-10-27T01:00:00Z",
      }],
    });
    const backend = installBackend({ skills: [item], session });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Retry matching" }));
    expect(await screen.findByRole("button", { name: "Change task" })).toBeVisible();
    expect(backend.getSession()).toMatchObject({
      id: session.id, task: session.task, status: "completed", feedback: session.feedback,
    });
    expect(screen.queryByRole("dialog", { name: "Edit task" })).not.toBeInTheDocument();
  });

  it("offers task editing when every available skill has been reviewed", async () => {
    const item = skill("design", "Thoughtful Design");
    const session = sessionFor([item], {
      feedback: [{
        id: "choice-1", session_id: "session-1", skill_id: item.id,
        action: "pass", reason: null, created_at: "2026-10-27T01:00:00Z",
      }],
    });
    const backend = installBackend({ skills: [item], session });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Change task" }));
    const dialog = screen.getByRole("dialog", { name: "Edit task" });
    expect(within(dialog).getByRole("textbox", { name: /What are you working on/ })).toHaveValue(session.task);
    expect(backend.getSession()).toEqual(session);
    expect(backend.getMatchRequests()).toEqual([]);
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

  it("restores a match accepted by the server when its response is lost without starting another job", async () => {
    const backend = installBackend({ session: null });
    const acceptMatch = vi.mocked(api.match).getMockImplementation()!;
    vi.mocked(api.match).mockImplementationOnce(async (...args) => {
      await acceptMatch(...args);
      throw new TypeError("The connection closed before the response arrived.");
    });
    const user = userEvent.setup();
    render(<App />);

    await user.type(await screen.findByRole("textbox", { name: /What are you working on/ }), "Plan an onboarding flow");
    await user.click(screen.getByRole("button", { name: "Find skills" }));

    expect(await screen.findByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Plan an onboarding flow" })).toBeVisible();
    expect(backend.getMatchRequests()).toEqual([{ task: "Plan an onboarding flow", agent: "codex" }]);
    expect(screen.queryByRole("button", { name: "Retry matching" })).not.toBeInTheDocument();
  });

  it("keeps cancellation authoritative when an earlier poll returns a running session", async () => {
    const previousSkill = skill("design", "Thoughtful Design");
    const running = sessionFor([previousSkill], { status: "running" });
    const backend = installBackend({ session: running });
    const stalePoll = deferred<Awaited<ReturnType<typeof api.session>>>();
    vi.mocked(api.session).mockReturnValueOnce(stalePoll.promise);
    const user = userEvent.setup();
    render(<App />);

    const stop = await screen.findByRole("button", { name: "Stop matching" });
    await waitFor(() => expect(api.session).toHaveBeenCalled(), { timeout: 2000 });
    await user.click(stop);
    expect(await screen.findByRole("heading", { name: "Thoughtful Design" })).toBeVisible();

    await act(async () => {
      stalePoll.resolve(running);
      await stalePoll.promise;
    });

    expect(backend.getSession()?.status).toBe("cancelled");
    expect(screen.getByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Save this skill" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Stop matching" })).not.toBeInTheDocument();
  });

  it("continues polling after a failed cancellation invalidates an outstanding poll", async () => {
    vi.useFakeTimers();
    const item = skill("design", "Thoughtful Design");
    const running = sessionFor([item], { status: "running" });
    installBackend({ skills: [item], session: running });
    const stalePoll = deferred<Awaited<ReturnType<typeof api.session>>>();
    vi.mocked(api.session)
      .mockReturnValueOnce(stalePoll.promise)
      .mockResolvedValue(sessionFor([item]));
    vi.mocked(api.cancel).mockRejectedValueOnce(new Error("Cancellation failed."));
    await act(async () => { render(<App />); });

    await act(async () => { await vi.advanceTimersToNextTimerAsync(); });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop matching" }));
    });
    expect(screen.getByRole("button", { name: "Retry cancellation" })).toBeEnabled();

    await act(async () => {
      stalePoll.resolve(running);
      await stalePoll.promise;
    });
    expect(screen.getByRole("button", { name: "Stop matching" })).toBeEnabled();
    await act(async () => { await vi.advanceTimersToNextTimerAsync(); });

    expect(screen.getByRole("heading", { name: item.name })).toBeVisible();
    expect(screen.getByRole("button", { name: "Save this skill" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Stop matching" })).not.toBeInTheDocument();
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
