import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import SkillCard from "./SkillCard";
import { sessionFor, skill } from "../test/fixtures";

const originalPointerCapture = Object.getOwnPropertyDescriptor(
  HTMLElement.prototype,
  "setPointerCapture",
);

afterEach(() => {
  if (originalPointerCapture) {
    Object.defineProperty(
      HTMLElement.prototype,
      "setPointerCapture",
      originalPointerCapture,
    );
  } else {
    delete (HTMLElement.prototype as Partial<HTMLElement>).setPointerCapture;
  }
});

function renderCard() {
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
    configurable: true,
    value: vi.fn(),
  });
  const item = skill("design", "Thoughtful Design");
  const onSwipe = vi.fn();
  const onOpen = vi.fn();
  const recommendation = sessionFor([item]).recommendations[0];
  render(
    <SkillCard
      skill={item}
      recommendation={recommendation}
      onOpen={onOpen}
      onSwipe={onSwipe}
    />,
  );
  return { card: screen.getByRole("article"), onOpen, onSwipe };
}

describe("skill card gestures", () => {
  it("commits a horizontal swipe as the matching card action", () => {
    const { card, onSwipe } = renderCard();
    fireEvent.pointerDown(card, { button: 0, pointerId: 1, clientX: 180, clientY: 80 });
    fireEvent.pointerMove(card, { pointerId: 1, clientX: 40, clientY: 82 });
    fireEvent.pointerUp(card, { pointerId: 1, clientX: 40, clientY: 82 });

    expect(onSwipe).toHaveBeenCalledTimes(1);
    expect(onSwipe).toHaveBeenCalledWith("pass");
  });

  it("does not commit vertical movement or a cancelled swipe", () => {
    const { card, onSwipe } = renderCard();
    fireEvent.pointerDown(card, { button: 0, pointerId: 1, clientX: 80, clientY: 40 });
    fireEvent.pointerMove(card, { pointerId: 1, clientX: 84, clientY: 110 });
    fireEvent.pointerUp(card, { pointerId: 1, clientX: 84, clientY: 110 });

    fireEvent.pointerDown(card, { button: 0, pointerId: 2, clientX: 180, clientY: 80 });
    fireEvent.pointerMove(card, { pointerId: 2, clientX: 40, clientY: 82 });
    fireEvent.pointerCancel(card, { pointerId: 2, clientX: 40, clientY: 82 });
    fireEvent.pointerUp(card, { pointerId: 2, clientX: 40, clientY: 82 });

    expect(onSwipe).not.toHaveBeenCalled();
  });

  it("leaves button gestures to the button action", () => {
    const { onOpen, onSwipe } = renderCard();
    const button = screen.getByRole("button", { name: "View skill & source" });
    fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 180, clientY: 80 });
    fireEvent.pointerMove(button, { pointerId: 1, clientX: 40, clientY: 82 });
    fireEvent.pointerUp(button, { pointerId: 1, clientX: 40, clientY: 82 });
    fireEvent.click(button);

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onSwipe).not.toHaveBeenCalled();
  });
});
