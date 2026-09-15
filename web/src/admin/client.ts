import type {
  AlertRule,
  AlertTest,
  Catalogue,
  Inspection,
  Mapping,
  Preview,
  Profile,
  ReportInfo,
  Run,
  SavedView,
} from "./types";

const BASE = "/api/admin";
const TOKEN = import.meta.env.VITE_API_TOKEN ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* non-JSON body */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

/** Files are sent base64 so the endpoint stays plain JSON. */
export function encodeFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
    reader.readAsDataURL(file);
  });
}

export const admin = {
  catalogue: () => request<Catalogue>("/catalogue"),

  inspect: (filename: string, content: string, fileFormat = "csv") =>
    request<Inspection>("/mapping/inspect", {
      method: "POST",
      body: JSON.stringify({ filename, content_base64: content, file_format: fileFormat }),
    }),

  preview: (filename: string, content: string, mappings: Mapping[], fileFormat = "csv") =>
    request<Preview>("/mapping/preview", {
      method: "POST",
      body: JSON.stringify({
        filename,
        content_base64: content,
        file_format: fileFormat,
        mappings,
      }),
    }),

  profiles: () => request<Profile[]>("/profiles"),
  saveProfile: (body: Record<string, unknown>) =>
    request<Profile>("/profiles", { method: "POST", body: JSON.stringify(body) }),

  runs: () => request<Run[]>("/runs"),

  views: () => request<SavedView[]>("/views"),
  saveView: (name: string, spec: Record<string, unknown>, description = "") =>
    request<SavedView>("/views", {
      method: "POST",
      body: JSON.stringify({ name, spec, description }),
    }),

  reports: () => request<ReportInfo[]>("/reports"),
  saveReport: (body: Record<string, unknown>) =>
    request<{ id: number }>("/reports", { method: "POST", body: JSON.stringify(body) }),
  runReport: (id: number) =>
    request<{ sent: string[]; attachments: { filename: string; bytes: number }[] }>(
      `/reports/${id}/run`,
      { method: "POST" },
    ),

  alerts: () => request<AlertRule[]>("/alerts"),
  saveAlert: (body: Record<string, unknown>) =>
    request<AlertRule>("/alerts", { method: "POST", body: JSON.stringify(body) }),
  deleteAlert: (id: number) => request<unknown>(`/alerts/${id}`, { method: "DELETE" }),
  testAlert: (body: Record<string, unknown>) =>
    request<AlertTest>("/alerts/test", { method: "POST", body: JSON.stringify(body) }),
};
