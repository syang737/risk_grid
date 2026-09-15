export interface FieldInfo {
  name: string;
  required: boolean;
  derived: boolean;
  derivedFrom: string[];
  default: unknown;
  description: string;
}

export interface TransformInfo {
  name: string;
  params: string[];
  description: string;
}

export interface ConnectorInfo {
  kind: string;
  settings: string[];
  description: string;
}

export interface Catalogue {
  fields: FieldInfo[];
  transforms: TransformInfo[];
  connectors: ConnectorInfo[];
}

export interface Mapping {
  field: string;
  source: string | null;
  constant?: unknown;
  transform: string;
  params: Record<string, unknown>;
}

export interface SourceColumn {
  name: string;
  samples: string[];
}

export interface Inspection {
  rows: number;
  columns: SourceColumn[];
  suggested: Mapping[];
}

export interface Finding {
  severity: "error" | "warning";
  code: string;
  message: string;
  count: number;
  sample: Record<string, unknown>[];
}

export interface Preview {
  ok: boolean;
  problems: string[];
  columns?: string[];
  rows: Record<string, unknown>[];
  findings: Finding[];
  validates?: boolean;
}

export interface Profile {
  id: number;
  name: string;
  version: number;
  active: boolean;
  connectorKind: string;
  connectorSettings: Record<string, unknown>;
  fileFormat: string;
  mappings: Mapping[];
  schedule: string;
  shockConfig: string;
  label: string;
}

export interface Run {
  id: number;
  fileName: string;
  status: "running" | "succeeded" | "quarantined" | "failed";
  rows: number;
  batchId: string;
  error: string;
  quarantineKey: string;
  findings: Finding[];
  startedAt: string | null;
  finishedAt: string | null;
}

export interface AlertRule {
  id: number;
  name: string;
  description: string;
  spec: {
    dimensions: string[];
    scope: Condition[];
    conditions: Condition[];
  };
  mode: "transition" | "every_batch";
  recipients: string[];
  active: boolean;
}

export interface Condition {
  column: string;
  op: string;
  value: number | string | null;
  value2?: number | string | null;
}

export interface AlertTest {
  description: string;
  batch: string;
  breaches: number;
  rows: Record<string, unknown>[];
}

export interface SavedView {
  id: number;
  name: string;
  description: string;
  spec: Record<string, unknown>;
}

export interface ReportInfo {
  id: number;
  name: string;
  viewId: number;
  schedule: string;
  recipients: string[];
  formats: string[];
  active: boolean;
  lastRunAt: string | null;
  lastError: string;
}
