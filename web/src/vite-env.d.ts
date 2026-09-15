/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_AG_GRID_LICENSE?: string;
  readonly VITE_API_TOKEN?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
