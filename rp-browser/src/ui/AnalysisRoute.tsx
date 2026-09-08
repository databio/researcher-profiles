/**
 * Guard for the Browse analysis views (Clusters, Topics, Search). With nothing
 * loaded these routes would dead-end, so we render an empty-state screen
 * instead of the page's broken content. A host application can replace that
 * screen through the `emptyScreen` shell slot, which is how a deployment with
 * accounts says "sign in" instead of "add a source".
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
