import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import Dialog from "./Dialog";

describe("dialog closing", () => {
  it("closes on the backdrop while leaving child and padding clicks alone", () => {
    const onClose = vi.fn();
    render(<Dialog title="Details" onClose={onClose}><button>Inside</button></Dialog>);
    const dialog = screen.getByRole("dialog", { name: "Details" });
    vi.spyOn(dialog, "getBoundingClientRect").mockReturnValue(new DOMRect(100, 100, 200, 200));

    fireEvent.click(screen.getByRole("button", { name: "Inside" }), { clientX: 90, clientY: 150 });
    fireEvent.click(dialog, { clientX: 150, clientY: 150 });
    fireEvent.click(dialog, { clientX: 100, clientY: 150 });
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(dialog, { clientX: 90, clientY: 150 });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("blocks backdrop, Escape, and button closing until closing is enabled", () => {
    const onClose = vi.fn();
    const { rerender } = render(<Dialog title="Details" onClose={onClose} closeDisabled>Content</Dialog>);
    const dialog = screen.getByRole("dialog", { name: "Details" });
    vi.spyOn(dialog, "getBoundingClientRect").mockReturnValue(new DOMRect(100, 100, 200, 200));

    fireEvent.click(dialog, { clientX: 90, clientY: 150 });
    const escape = new Event("cancel", { cancelable: true });
    fireEvent(dialog, escape);
    fireEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(escape.defaultPrevented).toBe(true);
    expect(onClose).not.toHaveBeenCalled();

    rerender(<Dialog title="Details" onClose={onClose}>Content</Dialog>);
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
