import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";

export default function Dialog({
  title,
  children,
  onClose,
  className = "",
  closeDisabled = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
  closeDisabled?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    // Capture focus before showModal moves it, so closing can return to the invoking control.
    const opener =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    dialog.showModal();
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
      dialog.close();
      if (opener?.isConnected) opener.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={`dialog ${className}`}
      onCancel={(event) => {
        // React owns removal so Escape follows the same close guard and cleanup as the button.
        event.preventDefault();
        if (!closeDisabled) onClose();
      }}
      aria-label={title}
      onClick={(event) => {
        if (closeDisabled || event.target !== event.currentTarget) return;
        // Dialog padding also targets the dialog; only a click outside its box is backdrop.
        const box = event.currentTarget.getBoundingClientRect();
        if (
          event.clientX < box.left ||
          event.clientX > box.right ||
          event.clientY < box.top ||
          event.clientY > box.bottom
        )
          onClose();
      }}
    >
      <div className="dialog-heading">
        <h2 className="dialog-title">{title}</h2>
        <button
          className="icon-button"
          onClick={onClose}
          disabled={closeDisabled}
          aria-label="Close dialog"
        >
          <X size={21} aria-hidden="true" />
        </button>
      </div>
      {children}
    </dialog>
  );
}
