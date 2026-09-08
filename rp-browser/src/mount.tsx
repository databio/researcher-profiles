/**
 * The app's entry point, as a function.
 *
 * `main.tsx` calls it with no options, which is the standalone app. A host
 * application calls it with its own routes and its own providers, so one
 * bundle can be both the plain static-host browser and the front end of a
 * larger service. Everything a host may add is declared in `slots.tsx`.
 */
import React, { type ReactNode } from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider } from "react-router";
import type { RouteObject } from "react-router";
import { createRouter } from "./routes";
import { ErrorBoundary } from "./ui/ErrorBoundary";
import "./styles/main.css";

export interface MountOptions {
  /** Routes a host application adds, mounted inside the shell layout. */
  routes?: RouteObject[];
  /**
   * Wrap the routed tree. A host puts its own providers here, including
   * <ShellSlotsProvider>, so slot values may read the host's own contexts.
   */
  wrap?: (children: ReactNode) => ReactNode;
}

export function mountApp(container: HTMLElement, opts: MountOptions = {}): void {
  const router = createRouter(opts.routes ?? []);
  const tree = <RouterProvider router={router} />;
  ReactDOM.createRoot(container).render(
    <ErrorBoundary>
      <React.StrictMode>{opts.wrap ? opts.wrap(tree) : tree}</React.StrictMode>
    </ErrorBoundary>,
  );
}
