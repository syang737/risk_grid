export interface Dimension {
  name: string;
  label: string;
  axis: "account" | "instrument";
}

export interface GridColumn {
  field: string;
  label: string;
  format: "integer" | "money" | "decimal" | "percent" | "text";
  width: number | null;
  /** The template asked for a scenario this batch's shock config never computed. */
  missing: boolean;
}

export interface ColumnsResponse {
  template: string;
  config: string;
  columns: GridColumn[];
}

export interface BatchInfo {
  id: string;
  label: string;
  displayName: string;
  timestamp: string;
  positions: number;
  shockConfig: string;
  scenarios: string[];
  cache: { entries: number; bytes: number };
}

export type Row = Record<string, unknown>;

export interface GridResponse {
  rows: Row[];
  lastRow: number;
  groupColumn: string | null;
  isLeaf: boolean;
  detailDimensions: string[];
  totals: Row | null;
}

export interface ShockAxis {
  count: number;
  step: number;
  symmetric: boolean;
  include_zero: boolean;
}

export interface ShockConfig {
  name: string;
  price_type: "sigma" | "pct";
  price: ShockAxis;
  vol: ShockAxis;
  horizon_days: number;
  by_strike: boolean;
}

export interface ColumnTemplate {
  name: string;
  columns: Array<Record<string, unknown>>;
}
