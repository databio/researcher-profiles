/// <reference types="vite/client" />

declare module "*.module.css" {
  const classes: { readonly [key: string]: string };
  export default classes;
}

interface ImportMetaEnv {
  readonly VITE_RPROFILES_BASE_URL?: string;
  readonly VITE_RPROFILES_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
