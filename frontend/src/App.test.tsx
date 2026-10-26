import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { api } from "./lib/api";
import { installBackend } from "./test/fixtures";

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("skill discovery", () => {
  it("saves a skill, advances the deck, and undoes both the choice and bookmark", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    expect(backend.getSaved().map((item) => item.skill.id)).toEqual(["design"]);
    await user.click(screen.getByRole("button", { name: "Undo last choice" }));
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    expect(backend.getSession().feedback).toEqual([]);
    expect(backend.getSaved()).toEqual([]);
  });

  it("records an explicit style rejection without saving the skill", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(
      screen.getByRole("button", { name: "Pass with a reason" }),
    );
    await user.click(screen.getByRole("button", { name: "Not my style" }));
    await screen.findByRole("heading", { name: "Clear Writing" });
    expect(backend.getSession().feedback[0]).toMatchObject({
      skill_id: "design",
      action: "pass",
      reason: "wrong-style",
    });
    expect(backend.getSaved()).toEqual([]);
  });

  it("keeps the current card when saving fails", async () => {
    installBackend();
    vi.mocked(api.feedback).mockRejectedValue(
      new Error("The local database is unavailable."),
    );
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Save this skill" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "database is unavailable",
    );
    expect(
      screen.getByRole("heading", { name: "Thoughtful Design" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Clear Writing" }),
    ).not.toBeInTheDocument();
  });

  it("does not swipe while arrow keys are used to edit a task", async () => {
    const backend = installBackend();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(
      screen.getByRole("textbox", { name: /What are you working on/ }),
    );
    await user.keyboard("{ArrowLeft}{ArrowRight}");
    expect(backend.getSession().feedback).toEqual([]);
    expect(
      screen.getByRole("heading", { name: "Thoughtful Design" }),
    ).toBeVisible();
  });

  it("browses and inspects source-backed skills without a Codex login", async () => {
    installBackend();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: "Explore" }));
    await user.type(
      screen.getByRole("textbox", { name: "Search skills" }),
      "Writing",
    );
    expect(
      screen.queryByRole("heading", { name: "Thoughtful Design" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Take a look" }));
    const dialog = screen.getByRole("dialog", { name: "Meet the skill" });
    expect(
      within(dialog).getByRole("heading", { name: "Clear Writing" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("link", { name: "Read the skill" }),
    ).toHaveAttribute(
      "href",
      "https://github.com/example/skills/blob/abc/writing/SKILL.md",
    );
  });

  it("saves explicit working and output preferences", async () => {
    installBackend();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Thoughtful Design" });
    await user.click(screen.getByRole("button", { name: /Make it more you/ }));
    const dialog = screen.getByRole("dialog", { name: "Your taste" });
    await user.click(
      within(dialog).getByRole("button", { name: "Think it through" }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Keep it short" }),
    );
    await user.type(
      within(dialog).getByRole("textbox"),
      "Warm, minimal visuals.",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Save my taste" }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(api.saveProfile).toHaveBeenCalledWith(
      expect.objectContaining({
        planning: "plan-first",
        communication: "concise",
        output_style: "Warm, minimal visuals.",
      }),
    );
    expect(
      screen.getByRole("button", { name: /Your taste is in the mix/ }),
    ).toBeVisible();
  });
});
