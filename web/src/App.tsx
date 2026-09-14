import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api/client";
import type { BatchInfo, ColumnTemplate, Dimension, GridColumn, ShockConfig } from "./api/types";
import { RiskGrid } from "./grid/RiskGrid";
import { DimensionPicker } from "./panels/DimensionPicker";
import { ShockConfigPanel } from "./panels/ShockConfigPanel";

// Account is deliberately left out of the drill order so it shows as a count
// chip on every instrument row -- clicking one is the cross-dimension drill.
const DEFAULT_DIMENSIONS = ["sector", "underlying", "contract"];
const DEFAULT_DETAIL = ["account", "desk"];

type Panel = "dimensions" | "shocks" | null;

export default function App() {
  const [dimensions, setDimensions] = useState<Dimension[]>([]);
  const [templates, setTemplates] = useState<ColumnTemplate[]>([]);
  const [configs, setConfigs] = useState<ShockConfig[]>([]);
  const [batch, setBatch] = useState<BatchInfo | null>(null);

  const [activeDimensions, setActiveDimensions] = useState(DEFAULT_DIMENSIONS);
  const [detailDimensions, setDetailDimensions] = useState(DEFAULT_DETAIL);
  const [template, setTemplate] = useState<string>("Exposure");
  const [measures, setMeasures] = useState<GridColumn[]>([]);
  const [panel, setPanel] = useState<Panel>("dimensions");
  const [error, setError] = useState<string | null>(null);

  const refreshMeta = useCallback(async () => {
    try {
      const [dims, tpls, cfgs, batches] = await Promise.all([
        api.dimensions(),
        api.templates(),
        api.configs(),
        api.batches(),
      ]);
      setDimensions(dims);
      setTemplates(tpls);
      setConfigs(cfgs);
      setBatch(batches[0] ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refreshMeta();
  }, [refreshMeta]);

  useEffect(() => {
    if (!batch) return;
    api
      .columns(batch.id, template)
      .then((payload) => setMeasures(payload.columns))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [batch, template]);

  const activeConfig = useMemo(
    () => configs.find((c) => c.name === batch?.shockConfig) ?? configs[0] ?? null,
    [configs, batch],
  );

  const missing = measures.filter((m) => m.missing);

  return (
    <div className="app">
      <header className="toolbar">
        <span className="brand">risk_grid</span>
        <span className="batch" title={batch ? `${batch.positions.toLocaleString()} positions` : ""}>
          {batch?.displayName ?? "loading…"}
        </span>

        <label className="toolbar-field">
          Template
          <select value={template} onChange={(e) => setTemplate(e.target.value)}>
            {templates.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
        </label>

        <label className="toolbar-field">
          Config
          <select value={batch?.shockConfig ?? ""} disabled>
            {configs.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}
              </option>
            ))}
          </select>
        </label>

        <span className="spacer" />

        {batch && (
          <span className="stat">
            {batch.positions.toLocaleString()} positions · {batch.scenarios.length} scenarios
          </span>
        )}
        <button
          type="button"
          className={panel === "dimensions" ? "tab active" : "tab"}
          onClick={() => setPanel(panel === "dimensions" ? null : "dimensions")}
        >
          Drill order
        </button>
        <button
          type="button"
          className={panel === "shocks" ? "tab active" : "tab"}
          onClick={() => setPanel(panel === "shocks" ? null : "shocks")}
        >
          Shocks
        </button>
      </header>

      {error && (
        <div className="banner error">
          {error}
          <button type="button" onClick={() => setError(null)}>
            dismiss
          </button>
        </div>
      )}
      {missing.length > 0 && (
        <div className="banner warn">
          This template asks for {missing.length} scenario
          {missing.length === 1 ? "" : "s"} the active shock config does not compute:{" "}
          {missing.map((m) => m.label).join(", ")}
        </div>
      )}

      <div className="body">
        <main>
          {dimensions.length > 0 && measures.length > 0 && batch ? (
            <RiskGrid
              dimensions={dimensions}
              activeDimensions={activeDimensions}
              detailDimensions={detailDimensions}
              measures={measures}
              batchId={batch.id}
              template={template}
              onDimensionsChange={setActiveDimensions}
              onError={setError}
            />
          ) : (
            <div className="panel-empty">Loading batch…</div>
          )}
        </main>

        {panel && (
          <aside>
            {panel === "dimensions" && (
              <DimensionPicker
                dimensions={dimensions}
                active={activeDimensions}
                detail={detailDimensions}
                onActiveChange={setActiveDimensions}
                onDetailChange={setDetailDimensions}
              />
            )}
            {panel === "shocks" && (
              <ShockConfigPanel
                config={activeConfig}
                positions={batch?.positions ?? 250_000}
                onRebuilt={() => void refreshMeta()}
                onError={setError}
              />
            )}
          </aside>
        )}
      </div>
    </div>
  );
}
