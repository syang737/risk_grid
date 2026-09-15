import { useCallback, useEffect, useMemo, useState } from "react";
import { admin, encodeFile } from "./client";
import type { Catalogue, Inspection, Mapping, Preview } from "./types";

/**
 * Onboarding a firm, as a form rather than a parser.
 *
 * The flow is upload a sample, confirm the guessed mapping, see what comes out,
 * save. Everything the incumbents do with bespoke code per clearing firm happens
 * here in configuration -- which is the whole strategic bet of the admin layer.
 */
export function MappingScreen({ onError }: { onError: (m: string | null) => void }) {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [filename, setFilename] = useState("");
  const [content, setContent] = useState("");
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [profileName, setProfileName] = useState("clearing-export");
  const [directory, setDirectory] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    admin.catalogue().then(setCatalogue).catch((e) => onError(String(e)));
  }, [onError]);

  const fieldNames = useMemo(
    () => (catalogue?.fields ?? []).map((f) => f.name),
    [catalogue],
  );
  const required = useMemo(
    () => (catalogue?.fields ?? []).filter((f) => f.required).map((f) => f.name),
    [catalogue],
  );
  const unmappedRequired = required.filter((f) => !mappings.some((m) => m.field === f));

  async function upload(file: File) {
    setBusy(true);
    setSaved(null);
    try {
      const encoded = await encodeFile(file);
      const result = await admin.inspect(file.name, encoded);
      setFilename(file.name);
      setContent(encoded);
      setInspection(result);
      setMappings(result.suggested);
      setPreview(null);
      onError(null);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const runPreview = useCallback(async () => {
    if (!content) return;
    setBusy(true);
    try {
      setPreview(await admin.preview(filename, content, mappings));
      onError(null);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [content, filename, mappings, onError]);

  // Previewing on every keystroke would be noisy; on every mapping change is
  // what makes the form feel like it is answering you.
  useEffect(() => {
    if (content && mappings.length) void runPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mappings, content]);

  function update(index: number, patch: Partial<Mapping>) {
    setMappings((current) =>
      current.map((m, i) => (i === index ? { ...m, ...patch } : m)),
    );
  }

  async function save() {
    setBusy(true);
    try {
      const profile = await admin.saveProfile({
        name: profileName,
        connector_kind: "local",
        connector_settings: directory ? { directory } : {},
        file_format: "csv",
        mappings,
      });
      setSaved(`Saved ${profile.name} v${profile.version}`);
      onError(null);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const errors = (preview?.findings ?? []).filter((f) => f.severity === "error");
  const warnings = (preview?.findings ?? []).filter((f) => f.severity === "warning");

  return (
    <div className="admin-page">
      <div className="admin-head">
        <h2>Onboard a data feed</h2>
        <p className="hint">
          Upload a sample of the firm's export. Columns are matched to the canonical schema
          where the names are recognisable; confirm or correct them, check the preview, and
          save. Saving creates a new version rather than editing in place, so batches already
          built stay reproducible.
        </p>
      </div>

      <section className="admin-block">
        <label className="file-drop">
          <input
            type="file"
            accept=".csv,.txt,.parquet"
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
          />
          <span>{filename || "Choose a sample file"}</span>
        </label>
        {inspection && (
          <span className="hint">
            {inspection.rows.toLocaleString()} rows, {inspection.columns.length} columns
          </span>
        )}
      </section>

      {inspection && catalogue && (
        <section className="admin-block">
          <div className="panel-head">Column mapping</div>
          {unmappedRequired.length > 0 && (
            <div className="banner warn inline">
              Still needed: {unmappedRequired.join(", ")}
            </div>
          )}
          <table className="mapping-table">
            <thead>
              <tr>
                <th>Their column</th>
                <th>Sample</th>
                <th>Our field</th>
                <th>Transform</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {mappings.map((mapping, index) => (
                <tr key={index}>
                  <td>
                    <select
                      value={mapping.source ?? ""}
                      onChange={(e) => update(index, { source: e.target.value })}
                    >
                      {inspection.columns.map((c) => (
                        <option key={c.name} value={c.name}>{c.name}</option>
                      ))}
                    </select>
                  </td>
                  <td className="muted">
                    {inspection.columns.find((c) => c.name === mapping.source)?.samples.join(", ")}
                  </td>
                  <td>
                    <select
                      value={mapping.field}
                      onChange={(e) => update(index, { field: e.target.value })}
                    >
                      {fieldNames.map((f) => (
                        <option key={f} value={f}>
                          {f}{required.includes(f) ? " *" : ""}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      value={mapping.transform}
                      onChange={(e) => update(index, { transform: e.target.value, params: {} })}
                    >
                      {catalogue.transforms.map((t) => (
                        <option key={t.name} value={t.name}>{t.name}</option>
                      ))}
                    </select>
                    <TransformParams
                      mapping={mapping}
                      catalogue={catalogue}
                      columns={inspection.columns.map((c) => c.name)}
                      onChange={(params) => update(index, { params })}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      onClick={() => setMappings((m) => m.filter((_, i) => i !== index))}
                    >
                      ×
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            type="button"
            onClick={() =>
              setMappings((m) => [
                ...m,
                {
                  field: unmappedRequired[0] ?? fieldNames[0],
                  source: inspection.columns[0]?.name ?? null,
                  transform: "trim",
                  params: {},
                },
              ])
            }
          >
            Add a mapping
          </button>
        </section>
      )}

      {preview && (
        <section className="admin-block">
          <div className="panel-head">Preview</div>
          {!preview.ok && (
            <div className="banner error inline">
              <ul>
                {preview.problems.map((p) => <li key={p}>{p}</li>)}
              </ul>
            </div>
          )}
          {errors.map((f) => (
            <div key={f.code} className="banner error inline">{f.message}</div>
          ))}
          {warnings.map((f) => (
            <div key={f.code} className="banner warn inline">{f.message}</div>
          ))}

          {preview.ok && preview.rows.length > 0 && (
            <div className="preview-scroll">
              <table className="mapping-table">
                <thead>
                  <tr>
                    {Object.keys(preview.rows[0]).map((k) => <th key={k}>{k}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.slice(0, 8).map((row, i) => (
                    <tr key={i}>
                      {Object.keys(preview.rows[0]).map((k) => (
                        <td key={k} className={typeof row[k] === "number" ? "num" : ""}>
                          {row[k] === null ? "" : String(row[k])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {inspection && (
        <section className="admin-block">
          <div className="panel-head">Save as a profile</div>
          <div className="form-row">
            <label>
              Name
              <input value={profileName} onChange={(e) => setProfileName(e.target.value)} />
            </label>
            <label>
              Watch directory
              <input
                value={directory}
                placeholder="/srv/drops/acme"
                onChange={(e) => setDirectory(e.target.value)}
              />
            </label>
            <button type="button" onClick={save} disabled={busy || !preview?.validates}>
              {busy ? "Working…" : "Save new version"}
            </button>
          </div>
          {!preview?.validates && (
            <p className="hint">
              Saving is disabled until the sample maps cleanly — a profile that cannot parse
              its own sample will quarantine every file it sees.
            </p>
          )}
          {saved && <div className="banner ok inline">{saved}</div>}
        </section>
      )}
    </div>
  );
}

/** Only the parameters the chosen transform actually takes. */
function TransformParams({
  mapping,
  catalogue,
  columns,
  onChange,
}: {
  mapping: Mapping;
  catalogue: Catalogue;
  columns: string[];
  onChange: (params: Record<string, unknown>) => void;
}) {
  const spec = catalogue.transforms.find((t) => t.name === mapping.transform);
  if (!spec?.params.length) return null;

  return (
    <div className="transform-params">
      {spec.params.map((param) => {
        const value = mapping.params?.[param];
        if (param === "side_column") {
          return (
            <label key={param}>
              {param}
              <select
                value={String(value ?? "")}
                onChange={(e) => onChange({ ...mapping.params, [param]: e.target.value })}
              >
                <option value="">—</option>
                {columns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
          );
        }
        return (
          <label key={param}>
            {param}
            <input
              value={
                value === undefined || value === null
                  ? ""
                  : typeof value === "object"
                    ? JSON.stringify(value)
                    : String(value)
              }
              onChange={(e) => {
                const raw = e.target.value;
                const parsed = param === "factor" ? Number(raw) || raw : raw;
                onChange({ ...mapping.params, [param]: parsed });
              }}
            />
          </label>
        );
      })}
    </div>
  );
}
