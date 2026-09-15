"""Turning a saved view into something you can put in an email.

A virtualised grid screenshots badly: only the visible rows exist in the DOM, so
capturing the real app gets one viewport of a report that should be a hundred
rows. Driving the SPA headless also means an authenticated browser context,
waiting on lazy block loads, and re-expanding the tree -- three things that fail
intermittently on a schedule nobody is watching.

So the report is rendered server-side as a plain table and screenshotted. The
cost is a second renderer whose styling has to stay recognisably like the grid's;
the benefit is that it is deterministic, needs no browser session, and can show
every row rather than every visible row.
"""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from risk.aggregate import PivotRequest, detail_count_column
from risk.templates import ColumnTemplate, ResolvedColumn, ShockConfig

# Matching web/src/styles.css, so a report looks like the screen it came from.
PALETTE = {
    "bg": "#12161c", "panel": "#171b22", "head": "#1f2530", "border": "#2b3340",
    "text": "#dce3ec", "muted": "#8b97a8", "accent": "#4c8dff",
    "pos": "#4ec9a5", "neg": "#ff6b6b",
}

MAX_ROWS = 500


@dataclass
class ViewSpec:
    """A saved pivot, in the shape the grid uses."""

    dimensions: tuple[str, ...] = ("sector",)
    detail_dimensions: tuple[str, ...] = ()
    filters: list[dict] = field(default_factory=list)
    sort: list[list] = field(default_factory=list)
    template: str = "Exposure"
    depth: int = 1
    max_rows: int = MAX_ROWS

    def to_pivot_request(self, measures: tuple[str, ...]) -> PivotRequest:
        from alerting import _filters_from

        return PivotRequest(
            dimensions=self.dimensions,
            filters=_filters_from(self.filters),
            measures=measures,
            detail_dimensions=tuple(self.detail_dimensions),
            sort=tuple((s[0], bool(s[1])) for s in self.sort),
        )

    def to_dict(self) -> dict:
        return {
            "dimensions": list(self.dimensions),
            "detail_dimensions": list(self.detail_dimensions),
            "filters": self.filters,
            "sort": self.sort,
            "template": self.template,
            "depth": self.depth,
            "max_rows": self.max_rows,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ViewSpec:
        return cls(
            dimensions=tuple(data.get("dimensions") or ("sector",)),
            detail_dimensions=tuple(data.get("detail_dimensions") or ()),
            filters=list(data.get("filters") or []),
            sort=[list(s) for s in (data.get("sort") or [])],
            template=data.get("template", "Exposure"),
            depth=int(data.get("depth", 1)),
            max_rows=int(data.get("max_rows", MAX_ROWS)),
        )


@dataclass
class RenderedRow:
    level: int
    label: str
    row: dict[str, Any]


def flatten(batch, request: PivotRequest, depth: int, max_rows: int) -> list[RenderedRow]:
    """Walk the pivot tree to `depth`, producing indented rows.

    A report of only the root level is rarely what anyone wants, and the whole
    tree is unbounded, so depth and a row cap are both explicit.
    """
    out: list[RenderedRow] = []

    def walk(current: PivotRequest, level: int) -> None:
        if level >= depth or len(out) >= max_rows:
            return
        result = batch.aggregate(current)
        for row in result.rows.to_dicts():
            if len(out) >= max_rows:
                return
            value = row.get(result.group_column)
            out.append(RenderedRow(level=level, label="" if value is None else str(value), row=row))
            if level + 1 < depth and not result.is_leaf and value is not None:
                walk(current.child(value), level + 1)

    walk(request, 0)
    return out


def _format(value: Any, fmt: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, (int, float)):
        return str(value)
    if fmt in ("money", "integer"):
        return f"{value:,.0f}"
    if fmt == "decimal":
        return f"{value:,.2f}"
    if fmt == "percent":
        return f"{value:.2%}"
    return str(value)


def _cell_class(value: Any) -> str:
    if not isinstance(value, (int, float)) or value == 0:
        return "num"
    return "num neg" if value < 0 else "num pos"


def render_html(
    batch,
    view: ViewSpec,
    template: ColumnTemplate,
    config: ShockConfig,
    title: str,
    totals: dict | None = None,
) -> str:
    """A self-contained HTML document of the view. No JavaScript, no lazy rows."""
    columns: list[ResolvedColumn] = [c for c in template.resolve(config.to_grid()) if not c.missing]
    request = view.to_pivot_request(template.measures())
    rows = flatten(batch, request, view.depth, view.max_rows)

    from risk.aggregate import DIMENSIONS

    group_header = " / ".join(DIMENSIONS[d].label for d in view.dimensions[: view.depth])
    detail_columns = [d for d in view.detail_dimensions if d not in view.dimensions]

    def header_cells() -> str:
        cells = [f'<th class="grouping">{html.escape(group_header)}</th>']
        cells += [f'<th class="dim">{html.escape(DIMENSIONS[d].label)}</th>' for d in detail_columns]
        cells.append('<th class="num">Positions</th>')
        cells += [f'<th class="num">{html.escape(c.label)}</th>' for c in columns]
        return "".join(cells)

    def detail_cell(row: dict, dim: str) -> str:
        value = row.get(dim)
        if value is not None:
            return f'<td class="dim">{html.escape(str(value))}</td>'
        count = row.get(detail_count_column(dim))
        if not count:
            return '<td class="dim"></td>'
        label = DIMENSIONS[dim].label.lower()
        plural = label if count == 1 else f"{label}s"
        return f'<td class="dim"><span class="chip">{count:,} {html.escape(plural)}</span></td>'

    def body_rows() -> str:
        out = []
        if totals:
            cells = ['<td class="grouping total">Total</td>']
            cells += ['<td class="dim"></td>' for _ in detail_columns]
            cells.append(f'<td class="num">{_format(totals.get("positions"), "integer")}</td>')
            for column in columns:
                value = totals.get(column.field)
                cells.append(f'<td class="{_cell_class(value)}">{_format(value, column.format)}</td>')
            out.append(f'<tr class="totals">{"".join(cells)}</tr>')

        for entry in rows:
            indent = entry.level * 18
            cells = [
                f'<td class="grouping" style="padding-left:{8 + indent}px">'
                f'{html.escape(entry.label)}</td>'
            ]
            cells += [detail_cell(entry.row, d) for d in detail_columns]
            cells.append(f'<td class="num">{_format(entry.row.get("positions"), "integer")}</td>')
            for column in columns:
                value = entry.row.get(column.field)
                cells.append(f'<td class="{_cell_class(value)}">{_format(value, column.format)}</td>')
            out.append(f'<tr class="lvl{entry.level}">{"".join(cells)}</tr>')
        return "".join(out)

    capped = (
        f'<p class="note">Showing the first {len(rows):,} rows; the view has more.</p>'
        if len(rows) >= view.max_rows else ""
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title><style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 20px; background: {PALETTE["bg"]}; color: {PALETTE["text"]};
         font: 12px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif; }}
  header {{ display: flex; align-items: baseline; gap: 14px; margin-bottom: 12px; }}
  h1 {{ font-size: 15px; margin: 0; color: {PALETTE["accent"]}; }}
  .meta {{ color: {PALETTE["muted"]}; }}
  table {{ border-collapse: collapse; width: 100%; background: {PALETTE["panel"]};
           border: 1px solid {PALETTE["border"]}; }}
  th, td {{ padding: 4px 8px; border-bottom: 1px solid {PALETTE["border"]}; white-space: nowrap; }}
  th {{ background: {PALETTE["head"]}; text-align: right; font-weight: 600;
        color: {PALETTE["muted"]}; text-transform: uppercase; font-size: 10px;
        letter-spacing: 0.05em; position: sticky; top: 0; }}
  th.grouping, th.dim {{ text-align: left; }}
  td.grouping {{ text-align: left; }}
  td.dim {{ text-align: left; color: {PALETTE["muted"]}; }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.num.pos {{ color: {PALETTE["pos"]}; }}
  td.num.neg {{ color: {PALETTE["neg"]}; }}
  tr.totals td {{ background: {PALETTE["head"]}; font-weight: 700;
                  border-bottom: 2px solid {PALETTE["border"]}; }}
  tr.lvl1 td.grouping {{ color: {PALETTE["muted"]}; }}
  .chip {{ border: 1px dashed {PALETTE["border"]}; border-radius: 9px;
           padding: 0 6px; font-size: 10.5px; color: {PALETTE["muted"]}; }}
  .note {{ color: {PALETTE["muted"]}; margin-top: 8px; }}
</style></head><body>
<header>
  <h1>{html.escape(title)}</h1>
  <span class="meta">{html.escape(batch.display_name)} &middot;
    {batch.positions:,} positions &middot; template {html.escape(template.name)} &middot;
    shocks {html.escape(config.name)}</span>
</header>
<table><thead><tr>{header_cells()}</tr></thead><tbody>{body_rows()}</tbody></table>
{capped}
<p class="note">Generated {datetime.now():%Y-%m-%d %H:%M} UTC by risk_grid.</p>
</body></html>"""


def render_csv(batch, view: ViewSpec, template: ColumnTemplate, config: ShockConfig) -> bytes:
    """The same view as data.

    Taken from the pivot rather than scraped from the rendered page, so the
    numbers are full precision rather than what happened to fit in a cell.
    """
    columns = [c for c in template.resolve(config.to_grid()) if not c.missing]
    request = view.to_pivot_request(template.measures())
    rows = flatten(batch, request, view.depth, view.max_rows)
    detail_columns = [d for d in view.detail_dimensions if d not in view.dimensions]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["level", "group"]
        + detail_columns
        + [f"{d}_count" for d in detail_columns]
        + ["positions"]
        + [c.label for c in columns]
    )
    for entry in rows:
        writer.writerow(
            [entry.level, entry.label]
            + [entry.row.get(d, "") for d in detail_columns]
            + [entry.row.get(detail_count_column(d), "") for d in detail_columns]
            + [entry.row.get("positions", "")]
            + [entry.row.get(c.field, "") for c in columns]
        )
    return buffer.getvalue().encode()


def capture_png(document: str, width: int = 1600, max_height: int = 4000) -> bytes:
    """Screenshot a rendered document.

    Raises rather than returning an empty image if Playwright is unavailable:
    a report that silently arrives without its screenshot is a bug nobody
    notices until someone asks where the numbers went.
    """
    from playwright.sync_api import sync_playwright

    import os

    executable = os.environ.get("RISK_GRID_CHROMIUM", "/opt/pw-browsers/chromium")
    with sync_playwright() as playwright:
        launch: dict = {}
        if os.path.exists(executable):
            launch["executable_path"] = executable
        browser = playwright.chromium.launch(**launch)
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.set_content(document, wait_until="load")
            height = min(page.evaluate("document.body.scrollHeight"), max_height)
            page.set_viewport_size({"width": width, "height": int(height) + 40})
            return page.screenshot(full_page=True)
        finally:
            browser.close()
