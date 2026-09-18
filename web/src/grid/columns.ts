import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { api } from "../api/client";
import type { Dimension, GridColumn } from "../api/types";
import { DetailCell, numberFormatter, signClass } from "./cells";

// Set filters need their options up front, which is only sane for dimensions
// with few distinct values. Accounts, instruments and contracts run to hundreds
// of thousands, so those get a text filter instead.
const LOW_CARDINALITY = new Set([
  "firm", "desk", "sector", "industry", "instrument_type", "expiry", "right",
]);

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
  const drillIndex = new Map(activeDimensions.map((name, index) => [name, index]));
  const shownAsColumn = new Set(
    detailDimensions.filter((name) => !drillIndex.has(name)),
  );

  // Every dimension gets a column, in use or not. A column is the only thing
  // AG Grid will let you drag into the row-group panel, so a dimension without
  // one could be dragged out of the drill order and never back in.
  const dimensionCols: ColDef[] = dimensions.map((dimension) => {
    const { name, label } = dimension;
    const base: ColDef = {
      colId: name,
      field: name,
      headerName: label,
      // What makes the row-group panel a drop target rather than a bin.
      enableRowGroup: true,
      width: 150,
      cellRenderer: DetailCell,
      cellRendererParams: { dimension: name, dimensionLabel: label, onDrill },
      ...dimensionFilter(name, batchId),
    };

    const index = drillIndex.get(name);
    if (index !== undefined) {
      // In the drill order: AG Grid renders it through the auto group column,
      // so the column itself is hidden in place.
      return { ...base, rowGroup: true, rowGroupIndex: index, hide: true };
    }
    // Every other state is the same column, shown or not. `rowGroup` is set
    // explicitly so re-applying the defs clears grouping that was dragged off.
    return {
      ...base,
      rowGroup: false,
      rowGroupIndex: null,
      hide: !shownAsColumn.has(name),
    };
  });

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

  return [...dimensionCols, countCol, ...measureCols];
}
