/**
 * The host-application extension points.
 *
 * This app is complete on its own: served from any static host it browses,
 * validates, and explains published researcher profiles with no server behind
 * it. A host application can also mount it (see `mount.tsx`) and add its own
 * chrome around it: extra nav links, an identity cluster, a different empty
 * screen, extra profile tabs. Those additions arrive through this context and
 * nowhere else, so the public bundle never carries host-specific code.
 *
 * Every slot is empty by default. Empty is the standalone app.
 */
import { createContext, useContext, type ReactNode } from "react";

/** Context handed to a host-supplied profile tab. */
export interface ProfileTabContext {
  /** The profile document URL the page is showing. */
  url: string;
  /** The profile's slug, once the document has loaded. */
  slug: string | null;
  /** Whether that URL is served from this origin. */
  sameOrigin: boolean;
}

export interface ProfileTabSlot {
  id: string;
  label: string;
  /** Plain predicate, no hooks: whether to offer this tab for this profile. */
  enabled: (ctx: ProfileTabContext) => boolean;
  render: (ctx: ProfileTabContext) => ReactNode;
}

export interface HomeAction {
  label: string;
  href: string;
  primary?: boolean;
}

/**
 * Chrome a host application can add when it embeds this SPA. Every field is
 * empty by default, which is the standalone static-host app. A host installs
 * its own values with <ShellSlotsProvider> inside mountApp's `wrap`.
 */
export interface ShellSlots {
  /** Extra links appended to the top nav. */
  navExtras: ReactNode;
  /** The right-hand header cluster (identity, sign in, sign out). */
  headerRight: ReactNode;
  /** Replaces the built-in zero-profile screen in Browse and the analysis views. */
  emptyScreen: ReactNode;
  /** Replaces the landing page's calls to action. */
  homeActions: HomeAction[] | null;
  /** A paragraph on the About page describing how this deployment is served. */
  aboutNote: ReactNode;
  /** Extra tabs on the profile page. */
  profileTabs: ProfileTabSlot[];
}

const EMPTY: ShellSlots = {
  navExtras: null,
  headerRight: null,
  emptyScreen: null,
  homeActions: null,
  aboutNote: null,
  profileTabs: [],
};

const Ctx = createContext<ShellSlots>(EMPTY);

export function ShellSlotsProvider({
  value,
  children,
}: {
  value: Partial<ShellSlots>;
  children: ReactNode;
}) {
  return <Ctx.Provider value={{ ...EMPTY, ...value }}>{children}</Ctx.Provider>;
}

export function useShellSlots(): ShellSlots {
  return useContext(Ctx);
}
