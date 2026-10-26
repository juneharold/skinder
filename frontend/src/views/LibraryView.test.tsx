import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { installBackend, savedItem, skill } from "../test/fixtures";

afterEach(() => vi.restoreAllMocks());

describe("saved library filters", () => {
  it("hides unused controls for an empty collection and lets the user explore", async () => {
    window.history.replaceState({}, "", "/?view=saved");
    installBackend({ session: null, saved: [] });
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("button", { name: "Explore skills" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Search skills" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Category" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Explore skills" }));
    expect(await screen.findByRole("heading", { name: "Thoughtful Design" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Search skills" })).toBeVisible();
  });

  it("keeps filters available when a saved collection has no matches", async () => {
    window.history.replaceState({}, "", "/?view=saved");
    const item = skill("design", "Thoughtful Design");
    installBackend({ session: null, saved: [savedItem(item)] });
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: item.name })).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Search skills" }), "missing");
    expect(screen.queryByRole("heading", { name: item.name })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Category" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("heading", { name: item.name })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Search skills" })).toHaveValue("");
  });

  it("lets users clear a restored filter even when the collection is empty", async () => {
    window.history.replaceState({}, "", "/?view=saved&q=missing");
    installBackend({ session: null, saved: [] });
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("button", { name: "Clear filters" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Search skills" })).toHaveValue("missing");
    expect(screen.getByRole("combobox", { name: "Category" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("button", { name: "Explore skills" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Search skills" })).not.toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).has("q")).toBe(false);
  });
});
