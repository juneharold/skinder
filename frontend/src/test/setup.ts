import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

beforeEach(() => window.history.replaceState({}, "", "/"));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
// Model modal visibility only; these stubs do not emulate the browser's native focus trap.
HTMLDialogElement.prototype.showModal = function () {
  this.setAttribute("open", "");
};
HTMLDialogElement.prototype.close = function () {
  this.removeAttribute("open");
};
