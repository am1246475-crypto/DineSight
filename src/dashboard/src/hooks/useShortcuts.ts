import { useEffect, useRef } from "react";

export interface Shortcuts {
  /** 0-based camera index for keys 1..8 */
  camera: (index: number) => void;
  grid: () => void;
  fullscreen: () => void;
}

/**
 * Global keyboard shortcuts: 1-8 open a camera, G = grid, F = fullscreen.
 * Uses KeyboardEvent.code (the physical key), so it works the same on an Arabic or English layout.
 * Ignored while typing in a field or when Ctrl/Alt/Cmd/Shift is held.
 */
export function useShortcuts(handlers: Shortcuts) {
  const ref = useRef(handlers);
  ref.current = handlers;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      const digit = /^(?:Digit|Numpad)([1-8])$/.exec(e.code);
      if (digit) { e.preventDefault(); ref.current.camera(Number(digit[1]) - 1); }
      else if (e.code === "KeyG") { e.preventDefault(); ref.current.grid(); }
      else if (e.code === "KeyF") { e.preventDefault(); ref.current.fullscreen(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
