import type { Dimension } from "../api/types";

interface Props {
  dimensions: Dimension[];
  active: string[];
  detail: string[];
  onActiveChange: (names: string[]) => void;
  onDetailChange: (names: string[]) => void;
}

const PRESETS: Array<{ label: string; dimensions: string[] }> = [
  { label: "By account", dimensions: ["desk", "master_account", "account", "underlying", "contract"] },
  { label: "By instrument", dimensions: ["sector", "industry", "underlying", "contract", "account"] },
  { label: "By expiry", dimensions: ["expiry", "underlying", "account"] },
];

export function DimensionPicker({
  dimensions,
  active,
  detail,
  onActiveChange,
  onDetailChange,
}: Props) {
  const byName = new Map(dimensions.map((d) => [d.name, d]));

  function move(index: number, delta: number) {
    const next = [...active];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    onActiveChange(next);
  }

  return (
    <div className="panel">
      <div className="panel-head">Drill order</div>
      <p className="hint">
        Rows group by these in order. Any order works — the four tabs of the old product are just
        four of these.
      </p>

      <div className="presets">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className={active.join() === preset.dimensions.join() ? "preset active" : "preset"}
            onClick={() => onActiveChange(preset.dimensions)}
          >
            {preset.label}
          </button>
        ))}
      </div>

      <ol className="dim-list">
        {active.map((name, index) => (
          <li key={name}>
            <span className="dim-name">{byName.get(name)?.label ?? name}</span>
            <span className="dim-actions">
              <button type="button" onClick={() => move(index, -1)} disabled={index === 0}>
                ↑
              </button>
              <button
                type="button"
                onClick={() => move(index, 1)}
                disabled={index === active.length - 1}
              >
                ↓
              </button>
              <button
                type="button"
                onClick={() => onActiveChange(active.filter((d) => d !== name))}
                disabled={active.length === 1}
              >
                ×
              </button>
            </span>
          </li>
        ))}
      </ol>

      <p className="hint">
        Add one here or drag it into the grouping bar above the grid — both write the same
        order.
      </p>
      <div className="chips">
        {dimensions
          .filter((d) => !active.includes(d.name))
          .map((d) => (
            <button
              key={d.name}
              type="button"
              className="toggle add"
              title={`Group by ${d.label} after ${byName.get(active[active.length - 1])?.label ?? "the last level"}`}
              onClick={() => onActiveChange([...active, d.name])}
            >
              + {d.label}
            </button>
          ))}
      </div>

      <div className="panel-head">Show as columns</div>
      <p className="hint">
        Dimensions not in the drill order. A row shows the value when it has only one, and a
        clickable count when it has more.
      </p>
      <div className="chips">
        {dimensions
          .filter((d) => !active.includes(d.name))
          .map((d) => (
            <button
              key={d.name}
              type="button"
              className={detail.includes(d.name) ? "toggle on" : "toggle"}
              onClick={() =>
                onDetailChange(
                  detail.includes(d.name)
                    ? detail.filter((n) => n !== d.name)
                    : [...detail, d.name],
                )
              }
            >
              {d.label}
            </button>
          ))}
      </div>
      <p className="hint">Each one costs roughly 15–20 ms per million positions.</p>
    </div>
  );
}
