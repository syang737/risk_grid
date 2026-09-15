import { useState } from "react";
import { api } from "../api/client";
import type { ShockAxis, ShockConfig } from "../api/types";

interface Props {
  config: ShockConfig | null;
  onSaved: () => void;
  onError: (message: string | null) => void;
}

function axisPoints(axis: ShockAxis): number[] {
  const positive = Array.from({ length: axis.count }, (_, i) => axis.step * (i + 1));
  const points = axis.symmetric ? [...positive.map((p) => -p).reverse(), ...positive] : positive;
  return axis.include_zero ? [...points, 0].sort((a, b) => a - b) : points;
}

function AxisEditor({
  title,
  axis,
  unit,
  onChange,
}: {
  title: string;
  axis: ShockAxis;
  unit: string;
  onChange: (axis: ShockAxis) => void;
}) {
  return (
    <div className="axis">
      <div className="axis-title">{title}</div>
      <label>
        Points per side
        <input
          type="number"
          min={1}
          max={8}
          value={axis.count}
          onChange={(e) => onChange({ ...axis, count: Number(e.target.value) })}
        />
      </label>
      <label>
        Step ({unit})
        <input
          type="number"
          step={0.01}
          min={0.01}
          value={axis.step}
          onChange={(e) => onChange({ ...axis, step: Number(e.target.value) })}
        />
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={axis.symmetric}
          onChange={(e) => onChange({ ...axis, symmetric: e.target.checked })}
        />
        Symmetric
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={axis.include_zero}
          onChange={(e) => onChange({ ...axis, include_zero: e.target.checked })}
        />
        Include unshocked
      </label>
      <div className="axis-preview">
        {axisPoints(axis)
          .map((p) => (unit === "sigma" ? `${p > 0 ? "+" : ""}${p}sd` : `${(p * 100).toFixed(0)}%`))
          .join("  ")}
      </div>
    </div>
  );
}

export function ShockConfigPanel({ config, onSaved, onError }: Props) {
  const [draft, setDraft] = useState<ShockConfig | null>(config);
  const [busy, setBusy] = useState(false);

  const current = draft ?? config;
  if (!current) return <div className="panel-empty">Loading shock config…</div>;

  const scenarios = axisPoints(current.price).length * axisPoints(current.vol).length;

  async function save() {
    if (!current) return;
    setBusy(true);
    try {
      await api.saveConfig(current);
      onError(null);
      onSaved();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">Shock config</div>

      <label className="row">
        Price moves in
        <select
          value={current.price_type}
          onChange={(e) =>
            setDraft({ ...current, price_type: e.target.value as ShockConfig["price_type"] })
          }
        >
          <option value="sigma">Historical stddev</option>
          <option value="pct">Flat percent</option>
        </select>
      </label>
      <p className="hint">
        {current.price_type === "sigma"
          ? "Each underlying moves by a multiple of its own daily stddev, so 2 sigma means something different for a utility than for a biotech."
          : "Every underlying moves by the same percentage, which hides the difference in how volatile they are."}
      </p>

      <AxisEditor
        title="Price"
        axis={current.price}
        unit={current.price_type === "sigma" ? "sigma" : "percent"}
        onChange={(price) => setDraft({ ...current, price })}
      />
      <AxisEditor
        title="Volatility"
        axis={current.vol}
        unit="percent"
        onChange={(vol) => setDraft({ ...current, vol })}
      />

      <label className="row">
        Horizon (days)
        <input
          type="number"
          min={0}
          step={1}
          value={current.horizon_days}
          onChange={(e) => setDraft({ ...current, horizon_days: Number(e.target.value) })}
        />
      </label>

      <div className="panel-foot">
        <span>{scenarios} scenarios</span>
        <button type="button" onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save config"}
        </button>
      </div>
      <p className="hint">
        Saving affects the next batch built against this config — repricing is the build
        worker's job, not the query service's, so it does not happen on this click. By-strike
        vol shocks are not implemented; the surface shifts in parallel only.
      </p>
    </div>
  );
}
