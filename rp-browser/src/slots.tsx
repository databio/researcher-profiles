/**
 * Host-application extension points. Host additions arrive only through this
 * context, so the public bundle never carries host-specific code. Every slot
 * is empty by default, which is the standalone app.
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

/** A host's branding for the top of the landing page. */
export interface HomeHero {
  /** Drawn large above the headline, in place of the app-name eyebrow. */
  mark: ReactNode;
  /** Inline content for the paragraph under the headline; replaces the default. */
  lede?: ReactNode;
  /** Shown below the calls to action, inside the hero. */
  extra?: ReactNode;
}

/**
 * Chrome a host can add when it embeds this SPA, installed with
 * <ShellSlotsProvider> inside mountApp's `wrap`.
 */
export interface ShellSlots {
  /** Replaces the built-in logo and app name at the left of the header. */
  brand: ReactNode;
  /** Extra links appended to the top nav. */
  navExtras: ReactNode;
  /** The right-hand header cluster (identity, sign in, sign out). */
  headerRight: ReactNode;
  /** Replaces the built-in zero-profile screen in Browse and the analysis views. */
  emptyScreen: ReactNode;
  /** A logo and lede for the landing page's hero. */
  homeHero: HomeHero | null;
  /** Replaces the landing page's calls to action. */
  homeActions: HomeAction[] | null;
  /** A paragraph on the About page describing how this deployment is served. */
  aboutNote: ReactNode;
  /** Extra tabs on the profile page. */
  profileTabs: ProfileTabSlot[];
}

const EMPTY: ShellSlots = {
  brand: null,
  navExtras: null,
  headerRight: null,
  emptyScreen: null,
  homeHero: null,
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
