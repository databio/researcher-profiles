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
  /** The profile's rid, once the document has loaded. */
  rid: string | null;
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
  /** Extra links beside About, on the right of the header. */
  navExtras: ReactNode;
  /**
   * Replaces the built-in links at the left of the header (Browse, Validate,
   * Skills); their routes stay either way. Style links with `navClass` from
   * AppLayout.
   */
  nav: ReactNode | null;
  /** The right-hand header cluster (identity, sign in, sign out). */
  headerRight: ReactNode;
  /** Show the Browse sources sidebar (source URLs, search tiers). */
  showSources: boolean;
  /** Replaces the built-in zero-profile screen in Browse and the analysis views. */
  emptyScreen: ReactNode;
  /** A logo and lede for the landing page's hero. */
  homeHero: HomeHero | null;
  /** Replaces the landing page's calls to action. */
  homeActions: HomeAction[] | null;
  /** Where the landing page sends a visitor instead (replacing history), or null to show it. */
  homeRedirect: string | null;
  /** A paragraph on the About page describing how this deployment is served. */
  aboutNote: ReactNode;
  /** Extra tabs on the profile page. */
  profileTabs: ProfileTabSlot[];
  /** Controls a host renders in the profile header (for example a Follow button). */
  profileActions: ((ctx: ProfileTabContext) => ReactNode) | null;
  /** Extra content at the end of the profile page's "Use with your AI" panel. */
  aiExtra: ((ctx: ProfileTabContext) => ReactNode) | null;
}

const EMPTY: ShellSlots = {
  brand: null,
  navExtras: null,
  nav: null,
  headerRight: null,
  showSources: true,
  emptyScreen: null,
  homeHero: null,
  homeActions: null,
  homeRedirect: null,
  aboutNote: null,
  profileTabs: [],
  profileActions: null,
  aiExtra: null,
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
