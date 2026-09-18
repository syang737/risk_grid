import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type {
  ColDef,
  ColumnRowGroupChangedEvent,
  ColumnVisibleEvent,
  GetRowIdParams,
  GridApi,
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
  onDetailDimensionsChange: (dimensions: string[]) => void;
  onError: (message: string | null) => void;
}

const same = (a: string[], b: string[]) =>
  a.length === b.length && a.every((value, index) => value === b[index]);

const groupedColIds = (api: GridApi) =>
  api.getRowGroupColumns().map((column) => column.getColId());

export function RiskGrid({
  dimensions,
  activeDimensions,
  detailDimensions,
  measures,
  batchId,
  template,
  onDimensionsChange,
  onDetailDimensionsChange,
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

  const dimensionNames = useMemo(() => dimensions.map((d) => d.name), [dimensions]);

  /** Move the ref first: AG Grid reloads rows before React re-renders. */
  const applyDimensions = useCallback(
    (next: string[]) => {
      contextRef.current = { ...contextRef.current, activeDimensions: next };
      onDimensionsChange(next);
    },
    [onDimensionsChange],
  );

  const handleDrill = useCallback(
    (dimension: string, params: ICellRendererParams) => {
      const node = params.node;
      const route: string[] = node.getRoute?.() ?? [];
      const depth = route.length;

      const next = contextRef.current.activeDimensions.filter((d) => d !== dimension);
      next.splice(depth, 0, dimension);

      pendingExpand.current = route;
      applyDimensions(next);
    },
    [applyDimensions],
  );

  /**
   * The row-group panel writes back here.
   *
   * Without this the panel was one-way: AG Grid would happily regroup itself
   * while `activeDimensions` -- which is what the datasource actually sends --
   * stayed as it was, so the grid and the server disagreed about what a row
   * meant. Dragging in, dragging out and reordering all raise this one event.
   */
  const onColumnRowGroupChanged = useCallback(
    (event: ColumnRowGroupChangedEvent) => {
      const next = groupedColIds(event.api);
      const current = contextRef.current.activeDimensions;

      if (next.length === 0) {
        // No grouping at all is a flat list of every position in the book, so
        // the last dimension does not come out -- same rule the Drill order
        // panel enforces on its own remove button.
        event.api.setRowGroupColumns(current);
        return;
      }
      if (same(next, current)) return;
      applyDimensions(next);
    },
    [applyDimensions],
  );

  /**
   * Showing a dimension column is the same act as toggling its "Show as
   * columns" chip, so the columns tool panel has to say so. Otherwise
   * un-hiding one there gives a column of blanks: the server is told
   * explicitly which detail dimensions to compute and would not have been
   * asked for this one.
   */
  const onColumnVisible = useCallback(
    (event: ColumnVisibleEvent) => {
      const grouped = new Set(groupedColIds(event.api));
      const known = new Set(dimensionNames);
      const visible = (event.api.getColumns() ?? [])
        .filter((column) => known.has(column.getColId()))
        .filter((column) => !grouped.has(column.getColId()) && column.isVisible())
        .map((column) => column.getColId());

      // A dimension in the drill order keeps its chip, so moving one in and
      // back out leaves the user's choice where they left it.
      const shown = new Set(visible);
      const current = contextRef.current.detailDimensions;
      const next = current.filter((name) => grouped.has(name) || shown.has(name));
      for (const name of visible) if (!next.includes(name)) next.push(name);

      if (same(next, current)) return;
      contextRef.current = { ...contextRef.current, detailDimensions: next };
      onDetailDimensionsChange(next);
    },
    [dimensionNames, onDetailDimensionsChange],
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

  /**
   * Refetch when the *request* changes but the row model does not.
   *
   * Detail dimensions and the column template are both computed server-side
   * and named in the request, so adding either only changes the column
   * definitions -- AG Grid has no reason to reload, and the new columns sit
   * there empty until something else happens to trigger a fetch. Changing the
   * drill order does not need this: that changes the grouping, which reloads
   * on its own.
   */
  const requestKey = `${detailDimensions.join("\u0000")}|${template ?? ""}`;
  const lastRequestKey = useRef(requestKey);
  useEffect(() => {
    if (lastRequestKey.current === requestKey) return;
    lastRequestKey.current = requestKey;
    // Not a purge: row ids are the node's full route, so expanded nodes keep
    // their place across the refresh.
    gridRef.current?.api?.refreshServerSide({ purge: false });
  }, [requestKey]);

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
        onColumnRowGroupChanged={onColumnRowGroupChanged}
        onColumnVisible={onColumnVisible}
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
