import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { api } from "../api/client";
import type { Dimension, GridColumn } from "../api/types";
import { DetailCell, numberFormatter, signClass } from "./cells";

// Set filters need their options up front, which is only sane for dimensions
// with few distinct values. Accounts, instruments and contracts run to hundreds
// of thousands, so those get a text filter instead.
const LOW_CARDINALITY = new Set(["firm", "desk", "sector", "instrument_type", "expiry", "right"]);

function dimensionFilter(name: string, batchId?: string): Partial<ColDef> {
  if (!LOW_CARDINALITY.has(name)) {
    return { filter: "agTextColumnFilter", filterParams: { maxNumConditions: 2 } };
  }
  return {
    filter: "agSetColumnFilter",
    filterParams: {
      values: (params: { success: (values: unknown[]) => void }) => {
        api
          .values(name, batchId)
          .then((payload) => params.success(payload.values))
          .catch(() => params.success([]));
      },
    },
  };
}

export interface BuildColumnsArgs {
  dimensions: Dimension[];
  activeDimensions: string[];
  detailDimensions: string[];
  measures: GridColumn[];
  batchId?: string;
  onDrill: (dimension: string, params: ICellRendererParams) => void;
}

export function buildColumnDefs({
  dimensions,
  activeDimensions,
  detailDimensions,
  measures,
  batchId,
  onDrill,
}: BuildColumnsArgs): ColDef[] {
  const byName = new Map(dimensions.map((d) => [d.name, d]));

  // Dimensions in the drill order become row-group columns; AG Grid renders
  // them through the auto group column, so they are hidden in place.
  const groupCols: ColDef[] = activeDimensions.map((name, index) => ({
    colId: name,
    field: name,
    headerName: byName.get(name)?.label ?? name,
    rowGroup: true,
    rowGroupIndex: index,
    hide: true,
    ...dimensionFilter(name, batchId),
  }));

  const detailCols: ColDef[] = detailDimensions
    .filter((name) => !activeDimensions.includes(name))
    .map((name) => ({
      colId: name,
      field: name,
      headerName: byName.get(name)?.label ?? name,
      width: 150,
      cellRenderer: DetailCell,
      cellRendererParams: {
        dimension: name,
        dimensionLabel: byName.get(name)?.label ?? name,
        onDrill,
      },
      ...dimensionFilter(name, batchId),
    }));

  const measureCols: ColDef[] = measures.map((col) => ({
    colId: col.field || col.label,
    field: col.field,
    headerName: col.label,
    width: col.width ?? 130,
    type: "rightAligned",
    filter: "agNumberColumnFilter",
    valueFormatter: numberFormatter(col.format),
    cellClass: signClass,
    // A template column its shock config cannot supply is shown struck through
    // rather than dropped, so the mismatch is visible instead of silent.
    headerClass: col.missing ? "missing-col" : undefined,
    sortable: !col.missing,
  }));

  const countCol: ColDef = {
    colId: "positions",
    field: "positions",
    headerName: "Positions",
    width: 100,
    type: "rightAligned",
    filter: "agNumberColumnFilter",
    valueFormatter: numberFormatter("integer"),
  };

  return [...groupCols, ...detailCols, countCol, ...measureCols];
}
