// @vitest-environment jsdom
/**
 * The app shell and the slots that shape it.
 *
 * What these pin:
 *  - the standalone browser keeps its own left links (Browse, Validate,
 *    Skills), and a host's `nav` slot replaces exactly those; About and
 *    `navExtras` stay on the right either way;
 *  - the header search shows once profiles are loaded and opens /browse?q=;
 *  - Browse reads its filter from ?q= and writes it back;
 *  - `showSources: false` hides the Browse sidebar;
 *  - `homeRedirect` sends the landing page elsewhere;
 *  - `profileActions` renders on each Browse row.
 */
import { describe, it, expect, afterEach, beforeEach } from "vitest";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider } from "react-router";
import { AppLayout } from "../src/AppLayout";
import { Home } from "../src/ui/Home";
import { Inventory } from "../src/ui/Inventory";
import { ShellSlotsProvider, type ShellSlots } from "../src/slots";
import { useStore, type ProfileCard } from "../src/store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SOURCE = "https://profiles.example.org/api/v1/collection.json";

function card(slug: string, name: string, over: Partial<ProfileCard> = {}): ProfileCard {
  return {
    slug, rid: null, name, level: "full", affiliation: null, field: null,
    paperCount: 0, summaryCount: 0, fulltextPct: 0,
    base: `https://profiles.example.org/api/v1/profiles/${slug}/content/`,
    sourceUrl: SOURCE, backendSpec: null, ...over,
  };
}

let root: Root | null = null;
let container: HTMLElement | null = null;
let router: ReturnType<typeof createMemoryRouter> | null = null;

beforeEach(() => {
  useStore.setState({ sources: [], cards: [], failures: [], centroids: new Map() });
});

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
    root = null;
  }
  container?.remove();
  container = null;
  router = null;
});

async function render(slots: Partial<ShellSlots> = {}, path = "/"): Promise<HTMLElement> {
  router = createMemoryRouter(
    [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <Home /> },
          { path: "browse", element: <Inventory /> },
          { path: "*", element: <p>elsewhere</p> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <ShellSlotsProvider value={slots}>
        <RouterProvider router={router!} />
      </ShellSlotsProvider>,
    );
  });
  return container;
}

const texts = (el: Element | null) =>
  [...(el?.querySelectorAll("a") ?? [])].map((a) => a.textContent);

const location = () => router!.state.location.pathname + router!.state.location.search;

describe("header nav", () => {
  it("shows Browse, Validate and Skills on the left when standalone", async () => {
    const el = await render({}, "/browse");
    const left = el.querySelector(".app-header__nav");
    expect(texts(left)).toEqual(["Browse", "Validate", "Skills"]);
    expect(left?.querySelector(".nav-link--active")?.textContent).toBe("Browse");
    expect(texts(el.querySelector('nav[aria-label="Help"]'))).toEqual(["About"]);
  });

  it("puts a host's nav in place of the built-in links", async () => {
    const el = await render({
      nav: <a className="nav-link" href="/mine">Mine</a>,
      navExtras: <a className="nav-link" href="/docs">Docs</a>,
    });
    expect(texts(el.querySelector(".app-header__nav"))).toEqual(["Mine"]);
    expect(texts(el.querySelector('nav[aria-label="Help"]'))).toEqual(["About", "Docs"]);
  });
});

describe("header search", () => {
  it("is hidden until profiles are loaded", async () => {
    const el = await render();
    expect(el.querySelector('form[role="search"]')).toBeNull();
  });

  it("opens Browse filtered by the text", async () => {
    useStore.setState({ cards: [card("ann", "Ann Lee"), card("bob", "Bob Ray")] });
    const el = await render({}, "/about");
    const input = el.querySelector('form[role="search"] input') as HTMLInputElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(input, "ann lee");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      (input.form as HTMLFormElement).requestSubmit();
    });
    expect(location()).toBe("/browse?q=ann%20lee");
    const rows = [...el.querySelectorAll("tbody tr")].map((r) => r.querySelector("td")?.textContent);
    expect(rows).toEqual(["Ann Lee"]);
  });
});

describe("Browse", () => {
  beforeEach(() => {
    useStore.setState({
      cards: [card("ann", "Ann Lee", { field: "Genomics" }), card("bob", "Bob Ray")],
    });
  });

  it("reads its filter from ?q= and writes it back", async () => {
    const el = await render({}, "/browse?q=genom");
    const filter = el.querySelector(".toolbar__filter") as HTMLInputElement;
    expect(filter.value).toBe("genom");
    expect(el.querySelectorAll("tbody tr")).toHaveLength(1);
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(filter, "");
      filter.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(location()).toBe("/browse");
    expect(el.querySelectorAll("tbody tr")).toHaveLength(2);
  });

  it("shows the sources sidebar unless the host hides it", async () => {
    expect((await render({}, "/browse")).querySelector(".app-sidebar")).not.toBeNull();
    act(() => root?.unmount());
    root = null;
    expect((await render({ showSources: false }, "/browse")).querySelector(".app-sidebar")).toBeNull();
  });

  it("renders the host's profile actions on each row", async () => {
    const el = await render(
      { profileActions: (ctx) => <button type="button">Follow {ctx.slug}</button> },
      "/browse",
    );
    const buttons = [...el.querySelectorAll("tbody td:last-child button")].map((b) => b.textContent);
    expect(buttons).toEqual(["Follow ann", "Follow bob"]);
    await act(async () => (el.querySelector("tbody td:last-child button") as HTMLButtonElement).click());
    expect(location()).toBe("/browse");
  });
});

describe("home", () => {
  it("shows the landing page by default", async () => {
    const el = await render();
    expect(el.querySelector(".home")).not.toBeNull();
  });

  it("goes to homeRedirect when the host sets one", async () => {
    const el = await render({ homeRedirect: "/me" });
    expect(location()).toBe("/me");
    expect(el.textContent).toContain("elsewhere");
  });
});
