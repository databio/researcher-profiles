/**
 * Guard for the Browse analysis views: with nothing loaded, render an empty
 * state (replaceable by the host's `emptyScreen` slot) instead.
 */
import type { ReactNode } from "react";
import { ShellState } from "./ShellState";
import { useStore } from "../store";
import { useShellSlots } from "../slots";

export function AnalysisRoute({ children }: { children: ReactNode }) {
  const { cards } = useStore();
  const slots = useShellSlots();
  if (cards.length === 0) {
    return (
      <>
        {slots.emptyScreen ?? (
          <ShellState
            title="Nothing to analyze yet."
            body="Add a profile source from the sidebar to get started."
          />
        )}
      </>
    );
  }
  return <>{children}</>;
}
