import type {
  BatchInfo,
  ColumnTemplate,
  ColumnsResponse,
  Dimension,
  GridResponse,
  ShockConfig,
} from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
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
  createBatch: (positions: number, config: string) =>
    request<{ id: string; displayName: string; positions: number }>("/batches", {
      method: "POST",
      body: JSON.stringify({ positions, config }),
    }),

  templates: () => request<ColumnTemplate[]>("/templates"),
  configs: () => request<ShockConfig[]>("/configs"),
  saveConfig: (config: ShockConfig) =>
    request<ShockConfig>("/configs", { method: "PUT", body: JSON.stringify(config) }),
};
