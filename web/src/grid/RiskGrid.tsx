import { useCallback, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type {
  ColDef,
  GetRowIdParams,
  GridReadyEvent,
  ICellRendererParams,
  IRowNode,
} from "ag-grid-community";
import { colorSchemeDarkBlue, themeQuartz } from "ag-grid-community";

import type { Dimension, GridColumn, Row } from "../api/types";
import { buildColumnDefs } from "./columns";
import { createDatasource, rowId } from "./datasource";
import { numberFormatter } from "./cells";

const theme = themeQuartz.withPart(colorSchemeDarkBlue).withParams({
  fontSize: 12,
  rowHeight: 26,
  headerHeight: 30,
  spacing: 5,
  backgroundColor: "#171b22",
  headerBackgroundColor: "#1f2530",
  oddRowBackgroundColor: "#1a1f27",
  borderColor: "#2b3340",
  accentColor: "#4c8dff",
});

export interface RiskGridProps {
  dimensions: Dimension[];
  activeDimensions: string[];
  detailDimensions: string[];
  measures: GridColumn[];
  batchId?: string;
  template?: string;
  onDimensionsChange: (dimensions: string[]) => void;
  onError: (message: string | null) => void;
}

export function RiskGrid({
  dimensions,
  activeDimensions,
  detailDimensions,
  measures,
  batchId,
  template,
  onDimensionsChange,
  onError,
}: RiskGridProps) {
  const gridRef = useRef<AgGridReact>(null);
  const [totals, setTotals] = useState<Row | null>(null);

  // Read through a ref so the datasource always sees current state without
  // being rebuilt -- rebuilding it would reset the grid's scroll and expansion.
  const contextRef = useRef({ activeDimensions, detailDimensions, batchId, template });
  contextRef.current = { activeDimensions, detailDimensions, batchId, template };

  // After a chip drill the dimension order changes, so the grid reloads. This
  // holds the route of the row that was clicked, to re-expand it once the new
  // data arrives -- otherwise the user's place is lost on every drill.
  const pendingExpand = useRef<string[] | null>(null);

  const handleDrill = useCallback(
    (dimension: string, params: ICellRendererParams) => {
      const node = params.node;
      const route: string[] = node.getRoute?.() ?? [];
      const depth = route.length;

      const next = contextRef.current.activeDimensions.filter((d) => d !== dimension);
      next.splice(depth, 0, dimension);

      pendingExpand.current = route;
      onDimensionsChange(next);
    },
    [onDimensionsChange],
  );

  const columnDefs = useMemo(
    () =>
      buildColumnDefs({
        dimensions,
        activeDimensions,
        detailDimensions,
        measures,
        batchId,
        onDrill: handleDrill,
      }),
    [dimensions, activeDimensions, detailDimensions, measures, batchId, handleDrill],
  );

  const datasource = useMemo(
    () =>
      createDatasource(() => ({
        dimensions: contextRef.current.activeDimensions,
        detailDimensions: contextRef.current.detailDimensions,
        batchId: contextRef.current.batchId,
        template: contextRef.current.template,
        onTotals: setTotals,
        onError,
      })),
    [onError],
  );

  const onGridReady = useCallback(
    (event: GridReadyEvent) => event.api.setGridOption("serverSideDatasource", datasource),
    [datasource],
  );

  /** Walk the saved route, expanding each node, once its level has loaded. */
  const onModelUpdated = useCallback(() => {
    const route = pendingExpand.current;
    if (!route?.length) return;

    const api = gridRef.current?.api;
    if (!api) return;

    let node: IRowNode | undefined;
    for (let i = 0; i < route.length; i += 1) {
      node = api.getRowNode(rowId(route.slice(0, i), route[i]));
      if (!node) return; // deeper levels not loaded yet; try again next update
      if (!node.expanded) api.setRowNodeExpanded(node, true);
    }
    if (node?.expanded) pendingExpand.current = null;
  }, []);

  const autoGroupColumnDef = useMemo<ColDef>(
    () => ({
      headerName: "Account / Instrument",
      width: 320,
      pinned: "left",
      cellRendererParams: {
        suppressCount: true,
        // The pinned row has no group key, so label it rather than leaving the
        // most prominent cell on screen blank.
        innerRenderer: (params: ICellRendererParams) =>
          params.node.rowPinned ? "Total" : (params.value ?? ""),
      },
    }),
    [],
  );

  const defaultColDef = useMemo<ColDef>(
    () => ({ sortable: true, resizable: true, suppressHeaderMenuButton: false }),
    [],
  );

  const pinnedTopRowData = useMemo(() => (totals ? [{ ...totals, __total: true }] : []), [totals]);

  return (
    <div className="grid-wrap">
      <AgGridReact
        ref={gridRef}
        theme={theme}
        columnDefs={columnDefs}
        defaultColDef={defaultColDef}
        autoGroupColumnDef={autoGroupColumnDef}
        rowModelType="serverSide"
        onGridReady={onGridReady}
        onModelUpdated={onModelUpdated}
        getRowId={(params: GetRowIdParams) => {
          const level = params.level ?? 0;
          const dimension = contextRef.current.activeDimensions[level];
          const key = dimension ? params.data?.[dimension] : params.data?.position_id;
          return rowId(params.parentKeys ?? [], String(key));
        }}
        pinnedTopRowData={pinnedTopRowData}
        getRowClass={(params) => (params.node.rowPinned ? "total-row" : undefined)}
        cacheBlockSize={100}
        maxBlocksInCache={20}
        blockLoadDebounceMillis={80}
        suppressAggFuncInHeader
        rowGroupPanelShow="always"
        groupDisplayType="singleColumn"
        animateRows={false}
        sideBar={{ toolPanels: ["columns", "filters"] }}
      />
      {pinnedTopRowData.length > 0 && (
        <div className="totals-hint">
          Totals cover the root level and reflect every active filter
          {typeof totals?.positions === "number"
            ? ` (${numberFormatter("integer")({ value: totals.positions } as never)} positions)`
            : ""}
        </div>
      )}
    </div>
  );
}
