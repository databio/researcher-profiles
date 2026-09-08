import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { viteStaticCopy } from "vite-plugin-static-copy";
import { resolve } from "path";

// Vite 8's native config loader deprecates CommonJS `__dirname`; use the ESM
// equivalent so the config keeps resolving sibling packages after the loader
// default flips.
const rootDir = import.meta.dirname;

const DEFAULT_APP_NAME = "Researcher Profile Browser";

/**
 * Inject the build-time app name into index.html's <title>. Lets a deployment
 * brand the served bundle as the registry it actually is, instead of shipping
 * the generic static-host name.
 */
function htmlAppName(appName: string): Plugin {
  return {
    name: "html-app-name",
    transformIndexHtml(html) {
      return html.replace(/__RP_APP_NAME__/g, appName);
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const appName = env.VITE_RP_APP_NAME || DEFAULT_APP_NAME;

  return {
    // Absolute base so the history (browser) router works: BrowserRouter needs
    // an absolute basename (derived from import.meta.env.BASE_URL in routes.tsx)
    // and deep-link SPA fallbacks need absolute asset URLs, not paths relative
    // to the requested route. A sub-path deployment overrides this at build
    // time, e.g. `vite build --base=/profiles/`, and both the asset URLs and
    // the router basename follow. (The old relative "./" base was for the
    // retired any-static-host hash router.)
    base: "/",
    plugins: [
      react(),
      htmlAppName(appName),
      viteStaticCopy({
        targets: [
          {
            src: "node_modules/onnxruntime-web/dist/*.{wasm,mjs}",
            dest: "ort",
          },
        ],
      }),
    ],
    define: {
      __RP_APP_NAME__: JSON.stringify(appName),
    },
    resolve: {
      alias: {
        "@rp/ui-lib": resolve(rootDir, "../rp-ui-lib/src/lib/index.ts"),
        "@rp/ui-lib/types": resolve(rootDir, "../rp-ui-lib/src/types.ts"),
        // The generated JSON Schemas, owned by rp-sdk and exported there by
        // `rp schema export schemas/`. There is exactly ONE tracked copy; the
        // explorer must never carry its own, because nothing would keep it in
        // sync with the pydantic models (rp-sdk/tests/test_schema.py pins the
        // SDK copy and only that one). A directory target, so this single
        // prefix entry covers every file under it.
        //
        // MIRRORED in tsconfig.json's `paths`: tsc does not read this file.
        // Edit both or typecheck and build disagree.
        "@rp/schemas": resolve(rootDir, "../rp-sdk/schemas"),
      },
      dedupe: ["react", "react-dom"],
    },
    server: {
      port: 5185,
      fs: {
        // Sibling packages are resolved through the aliases above and live
        // outside this project root: rp-ui-lib/src and rp-sdk/schemas. The dev
        // server refuses to serve either without this.
        allow: [resolve(rootDir, "..")],
      },
    },
    build: {
      target: "esnext",
    },
  };
});
