import type { ICellRendererParams, ValueFormatterParams } from "ag-grid-community";
import type { GridColumn } from "../api/types";

const FORMATTERS: Record<GridColumn["format"], Intl.NumberFormat> = {
  money: new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }),
  integer: new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }),
  decimal: new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  percent: new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 }),
  text: new Intl.NumberFormat("en-US"),
};

export function numberFormatter(format: GridColumn["format"]) {
  return (params: ValueFormatterParams) => {
    const value = params.value;
    if (value === null || value === undefined || value === "") return "";
    if (typeof value !== "number") return String(value);
    // A rounded-to-zero value is not zero, and on a risk screen the difference
    // between "nothing here" and "small" matters.
    if (value !== 0 && Math.abs(value) < 0.5 && format !== "decimal") {
      return value > 0 ? "~0" : "~-0";
    }
    return FORMATTERS[format].format(value);
  };
}

/** Red below zero, green above -- matching how the incumbent colours risk. */
export function signClass(params: { value: unknown }) {
  if (typeof params.value !== "number" || params.value === 0) return "num";
  return params.value < 0 ? "num neg" : "num pos";
}

export interface DetailCellParams extends ICellRendererParams {
  dimension: string;
  dimensionLabel: string;
  onDrill: (dimension: string, params: ICellRendererParams) => void;
}

/**
 * A dimension column that is not the current grouping key.
 *
 * The incumbent renders `[21]` here, which tells you nothing and does nothing.
 * When the group holds exactly one value we show it; otherwise we show a chip
 * that says what the count is *of* and, when clicked, regroups by that
 * dimension underneath this row. That is how cross-dimension drill is invoked:
 * from an instrument row, "21 accounts" takes you to the 21 accounts holding it.
 */
export function DetailCell(params: DetailCellParams) {
  const value = params.data?.[params.dimension];
  const count = params.data?.[`${params.dimension}__n`] as number | undefined;

  if (value !== null && value !== undefined) return <span>{String(value)}</span>;
  if (!count) return null;

  const noun = count === 1 ? params.dimensionLabel : `${params.dimensionLabel}s`;
  return (
    <button
      type="button"
      className="chip"
      title={`Group these ${count} ${noun.toLowerCase()} under this row`}
      onClick={(event) => {
        event.stopPropagation();
        params.onDrill(params.dimension, params);
      }}
    >
      {count.toLocaleString()} {noun.toLowerCase()}
    </button>
  );
}
