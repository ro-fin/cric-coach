/**
 * Keyboard-shortcut guard (US-K1: ←/→ frame-step, ↑/↓ ball-step, number keys
 * switch camera). Shortcuts must never hijack typing in form controls.
 */

const EDITABLE_TAGS = new Set(["INPUT", "SELECT", "TEXTAREA", "OPTION"]);

/** True when the event target is a form control that owns its key input. */
export function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && EDITABLE_TAGS.has(target.tagName);
}
