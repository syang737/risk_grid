"""Drive the grid's drill order from a browser.

    python -m uvicorn api.main:app --port 8000
    cd web && npm run dev
    python spikes/grid_interaction.py

Not part of `pytest`: it needs a running API, a running dev server and a
built batch, so it is run by hand. `RISK_GRID_WEB` overrides the dev server
URL and `CHROMIUM` the browser binary.

The assertions are against the network, not the DOM. Dragging a chip used to
regroup the grid locally while the request kept the old dimension order, so a
passing screenshot proved nothing -- what matters is that the *server* was
asked to group by the dimension that was dropped.
"""
import json
import os
import sys
from playwright.sync_api import sync_playwright

URL = os.environ.get("RISK_GRID_WEB", "http://localhost:5173/")
posts: list[dict] = []


def run(page):
    page.wait_for_selector(".ag-row", timeout=30_000)
    page.wait_for_timeout(1500)

    # 1. The columns tool panel must list every dimension.
    page.click(".ag-side-button:has-text('Columns')")
    page.wait_for_selector(".ag-column-select-column-label", timeout=10_000)
    page.wait_for_timeout(800)
    listed = [t.strip() for t in page.locator(
        ".ag-column-select-list .ag-column-select-column-label").all_inner_texts()]
    print("tool panel columns:", listed)

    for label in ("Firm", "Desk", "Master Account", "Strike", "Right", "Type", "Industry"):
        assert label in listed, f"{label} missing from the columns tool panel"

    # 2. Drag Desk from the tool panel into the row-group panel.
    posts.clear()
    source = page.locator(
        ".ag-column-select-list .ag-column-select-column",
        has=page.locator(".ag-column-select-column-label", has_text="Desk")
    ).first.locator(".ag-column-select-column-drag-handle")
    target = page.locator(".ag-column-drop-horizontal-rowgroup").first
    # AG Grid drives drags off its own mouse service, not HTML5 drag events,
    # so this walks the pointer rather than calling drag_to.
    start, end = source.bounding_box(), target.bounding_box()
    page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
    page.mouse.down()
    # AG Grid ignores a drag until the pointer clears its 4px threshold.
    page.mouse.move(start["x"] + start["width"] / 2 + 8,
                    start["y"] + start["height"] / 2 + 8)
    page.wait_for_timeout(120)
    for step in range(1, 11):
        page.mouse.move(
            start["x"] + (end["x"] + end["width"] - 40 - start["x"]) * step / 10,
            start["y"] + (end["y"] + end["height"] / 2 - start["y"]) * step / 10,
        )
        page.wait_for_timeout(40)
    page.mouse.up()
    page.wait_for_timeout(2500)

    grouped = [t.strip() for t in page.locator(
        ".ag-column-drop-horizontal-rowgroup .ag-column-drop-cell-text").all_inner_texts()]
    print("row-group panel after drag-in:", grouped)
    assert "Desk" in grouped, "Desk did not land in the row-group panel"

    dims = [p["dimensions"] for p in posts]
    print("server asked for:", dims[-1] if dims else None)
    assert dims and "desk" in dims[-1], f"server never grouped by desk: {dims}"

    # 3. The Drill order panel agrees.
    drill = [t.strip() for t in page.locator(".dim-list .dim-name").all_inner_texts()]
    print("drill order panel:", drill)
    assert "Desk" in drill, "the side panel did not follow the grid"

    # 4. And the rows really are desks.
    first = page.locator(".ag-row").first.inner_text().strip().replace("\n", " / ")
    print("first group cell:", first)

    # 5. Drag it back out.
    posts.clear()
    chip = page.locator(".ag-column-drop-horizontal-rowgroup .ag-column-drop-cell",
                        has_text="Desk").first
    chip.locator(".ag-column-drop-cell-button").click()
    page.wait_for_timeout(2500)

    dims = [p["dimensions"] for p in posts]
    print("server asked for:", dims[-1] if dims else None)
    assert dims and "desk" not in dims[-1], f"desk still grouped: {dims}"
    drill = [t.strip() for t in page.locator(".dim-list .dim-name").all_inner_texts()]
    assert "Desk" not in drill, "the side panel kept a level the grid dropped"
    print("drill order panel:", drill)

    # 6. The last level cannot be dragged out.
    while len(page.locator(".ag-column-drop-horizontal-rowgroup .ag-column-drop-cell").all()) > 1:
        page.locator(".ag-column-drop-horizontal-rowgroup .ag-column-drop-cell").first \
            .locator(".ag-column-drop-cell-button").click()
        page.wait_for_timeout(1200)
    page.locator(".ag-column-drop-horizontal-rowgroup .ag-column-drop-cell").first \
        .locator(".ag-column-drop-cell-button").click()
    page.wait_for_timeout(1500)
    left = [t.strip() for t in page.locator(
        ".ag-column-drop-horizontal-rowgroup .ag-column-drop-cell-text").all_inner_texts()]
    print("after trying to remove the last level:", left)
    assert len(left) == 1, "the grid was left with no grouping at all"

    # 7. Add one back from the side panel.
    import re

    page.locator(".chips .toggle.add").filter(
        has_text=re.compile(r"^\+\s*Account$")).first.click()
    page.wait_for_timeout(2000)
    left = [t.strip() for t in page.locator(
        ".ag-column-drop-horizontal-rowgroup .ag-column-drop-cell-text").all_inner_texts()]
    print("after the side panel add:", left)
    assert "Account" in left, "the side panel's add did not reach the grid"

    # 8. Un-hiding a dimension in the columns tool panel has to reach the
    #    server as a detail dimension, or the column renders blank.
    posts.clear()
    if page.locator(".ag-column-select-column-label").count() == 0:
        page.click(".ag-side-button:has-text('Columns')")
        page.wait_for_selector(".ag-column-select-column-label", timeout=10_000)
        page.wait_for_timeout(800)
    page.locator(
        ".ag-column-select-list .ag-column-select-column",
        has=page.locator(".ag-column-select-column-label", has_text="Right"),
    ).first.locator(".ag-checkbox-input-wrapper").click()
    page.wait_for_timeout(2500)

    detail = [p.get("detail_dimensions") for p in posts]
    print("server detail dimensions:", detail[-1] if detail else None)
    assert detail and "right" in detail[-1], f"never asked for right: {detail}"
    chips = [t.strip() for t in page.locator(".chips .toggle.on").all_inner_texts()]
    print("chips on:", chips)
    assert "Right" in chips, "the side panel's chip did not follow the tool panel"


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    page.on("console", lambda m: m.type == "error" and print("console error:", m.text))

    def on_request(request):
        if request.url.endswith("/api/grid/rows") and request.method == "POST":
            try:
                posts.append(json.loads(request.post_data or "{}"))
            except ValueError:
                pass

    page.on("request", on_request)
    page.goto(URL)
    try:
        run(page)
        print("\nPASS")
    except Exception as exc:
        page.screenshot(path="grid-drag-failure.png", full_page=True)
        print("\nFAIL:", exc)
        sys.exit(1)
    finally:
        browser.close()
