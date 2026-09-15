import type {
  BatchInfo,
  ColumnTemplate,
  ColumnsResponse,
  Dimension,
  GridResponse,
  ShockConfig,
} from "./types";

const BASE = "/api";

// Every call is authenticated and resolves to exactly one firm server-side.
// In dev the token comes from VITE_API_TOKEN (see `python -m control.bootstrap`);
// a real deployment puts a session cookie or an SSO exchange here instead.
const TOKEN = import.meta.env.VITE_API_TOKEN ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (response.status === 401) {
    throw new Error("Not authenticated - set VITE_API_TOKEN to a valid API key");
  }
  if (!response.ok) {
    // The API puts the reason in `detail` -- surface it rather than a bare
    // status, since most failures here are a malformed pivot request that the
    // message names precisely.
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export interface RowsRequest {
  startRow: number;
  endRow: number;
  dimensions: string[];
  groupKeys: unknown[];
  filterModel: Record<string, unknown>;
  sortModel: Array<{ colId: string; sort: string }>;
  detail_dimensions: string[];
  batch_id?: string;
  template?: string;
  include_totals?: boolean;
}

export const api = {
  rows: (body: RowsRequest) =>
    request<GridResponse>("/grid/rows", { method: "POST", body: JSON.stringify(body) }),

  columns: (batchId?: string, template?: string) => {
    const params = new URLSearchParams();
    if (batchId) params.set("batch_id", batchId);
    if (template) params.set("template", template);
    return request<ColumnsResponse>(`/grid/columns?${params}`);
  },

  values: (column: string, batchId?: string) => {
    const params = new URLSearchParams();
    if (batchId) params.set("batch_id", batchId);
    return request<{ column: string; values: unknown[] }>(
      `/grid/values/${encodeURIComponent(column)}?${params}`,
    );
  },

  dimensions: () => request<Dimension[]>("/dimensions"),
  batches: () => request<BatchInfo[]>("/batches"),
  templates: () => request<ColumnTemplate[]>("/templates"),
  configs: () => request<ShockConfig[]>("/configs"),
  saveConfig: (config: ShockConfig) =>
    request<ShockConfig>("/configs", { method: "PUT", body: JSON.stringify(config) }),
};
