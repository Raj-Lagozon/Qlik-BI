---
name: qlik-sheets-to-pbir
description: Convert Qlik sheets and their chart/KPI/table objects, including x/y/w/h layout, into Power BI PBIR page.json and visual.json files
---

# Role
Convert one Qlik sheet (with its objects) into one Power BI report **page**
(`definition/pages/<PageId>/page.json`) plus one `visual.json` per chart/KPI/
table/filter object on that sheet, preserving type, bound fields/measures and
canvas position.

# Critical: preserve exact names
Every table name, field name, and measure name in a `SourceRef.Entity` or
`Property` value must be **copied verbatim** from the DAX measure names and
dimension/hierarchy names you were given (from the other conversion steps)
or from the sheet's own field references — same spelling, same casing.
Never invent a name, never normalize/clean one up, and never substitute a
name from this document's own illustrative examples (`<Table>`, `<Field>`,
`<MeasureName>` are placeholders explaining the *shape*, not real names to
fall back on — and neither is any concrete table/field name mentioned only
as an example elsewhere in this file). A renamed or invented field/table
name breaks silently — the visual looks plausible right up until Power BI
shows "fields that need to be fixed."

# Input you receive
One entry from `sheets.json` (`id`, `title`, `objects[]` — each with `type`,
`bounds {x,y,width,height}`, `layout.properties`, `layout.layout`), plus the
DAX measure names and dimension/hierarchy names already produced by the other
skills (so you reference the *converted* names, not the raw Qlik expressions).

# Output — one JSON object per sheet
```json
{
  "page": {"name": "<PageId>", "displayName": "<sheet title>", "width": 1280, "height": 720, "ordinal": <rank>},
  "visuals": [
    {
      "name": "<objectId>",
      "position": {"x": <px>, "y": <px>, "z": <ordinal>, "width": <px>, "height": <px>, "tabOrder": <ordinal>},
      "visual": {
        "visualType": "<pbi visual type>",
        "query": {"queryState": { "<Role>": {"projections": [{"field": {...}, "queryRef": "<Table>.<Field>"}]}}},
        "objects": {}
      },
      "confidence": "high",
      "notes": "<optional — anything that needs manual verification>"
    }
  ]
}
```
`confidence` is `"high"`/`"medium"`/`"low"` — use `"low"` for an
unrecognized extension type you bound by best-effort shape-matching, a
threshold/conditional-formatting rule you couldn't express exactly, or a
tab-container split you're not fully sure about.
`notes` is optional per visual — include it whenever something couldn't be
converted deterministically (an unrecognized extension type, a threshold
rule that isn't expressible as simple conditional formatting, etc.) so a
person knows to check that one visual by hand; the builder strips it before
writing the real PBIR file. This is intentionally close to real PBIR
`page.json`/`visual.json` shape —
the builder writes it out mostly as-is, only filling in boilerplate
(`$schema`, `annotations`, GUID-safe names).

# Layout: Qlik grid → Power BI canvas pixels
Qlik sheets use a 24 (or similar) column grid with `bounds` sometimes given as
row/col spans instead of pixels. Normalize:
- If `bounds` already has `x/y/width/height` in pixels (Qlik's newer
  responsive layout), use them directly, but rescale so the sheet's full
  width maps to Power BI's default page width of **1280** (and height to
  **720**, or **1920x1080** if the Qlik sheet metadata indicates a widescreen
  layout) — i.e. `pbi_x = qlik_x / qlik_sheet_width * 1280`, same for y/w/h.
- If `bounds` are grid cells (`col`/`row`/`colspan`/`rowspan` on a 24-column
  grid), convert: `pbi_x = col / 24 * 1280`, `pbi_width = colspan / 24 * 1280`,
  and similarly for y/height against a assumed 12-row sheet height mapped to
  720.
- `tabOrder`/`z` = the object's index in the sheet's `cells` array (already
  its natural stacking/tab order).

# Visual type mapping (Qlik object `type` → Power BI `visualType`)
| Qlik | Power BI |
|---|---|
| `barchart` (vertical) | `clusteredColumnChart` (or `columnChart` for stacked — check `layout.properties.stacked`) |
| `barchart` (horizontal) | `clusteredBarChart` |
| `linechart` | `lineChart` |
| `combochart` | `lineClusteredColumnComboChart` |
| `piechart` | `pieChart` |
| `treemap` | `treemap` |
| `scatterplot` | `scatterChart` |
| `kpi` | `card` (single measure) or `multiRowCard` (multiple measures) |
| `table` | `tableEx` |
| `pivot-table` | `pivotTable` |
| `gauge` | `gauge` |
| `map` (area/point) | `map` or `filledMap` depending on `layout.properties.mapType` |
| `listbox` | `slicer` (list style) |
| `filterpane` | one `slicer` per contained field |
| `text-image` | `textbox` |
| `sn-table` (native Sense table) | `tableEx` |

If a Qlik object type has no reasonable Power BI equivalent (e.g. a custom
extension object) **and it has no `qHyperCubeDef` of its own** (no real
dimensions/measures bound to it — a pure decorative/config extension), emit
a `textbox` visual containing a note of the original object's title and
type instead of dropping it silently.

A `textbox`'s text is a `general` object's `properties.paragraphs`, never a
top-level `objects.paragraphs` and never a bare `{"text": "..."}`:
```json
"visual": {
  "visualType": "textbox",
  "objects": {
    "general": [
      {"properties": {"paragraphs": [{"textRuns": [{"value": "<the text>"}]}]}}
    ]
  }
}
```
`objects.paragraphs` directly (skipping `general`/`properties`) is invalid
PBIR — Power BI rejects the whole report ("Required property 'properties'
was not included" / "An additional property 'text' was included").

If an unrecognized `type` string DOES carry a real `qHyperCubeDef` with
dimensions/measures (a third-party or custom-branded chart extension built
on standard Qlik data binding, just not one of the types in the table
above), don't fall back to a blank textbox and throw that data away — bind
it the same way the closest matching known type would: one measure only →
`card`; one dimension + one measure → `clusteredColumnChart`; one dimension
+ multiple measures → `clusteredColumnChart` with multiple `"Y"`
projections. Note the original unrecognized type in `"notes"` so a person
knows the visual style is a best-effort substitute, but the data binding
itself is real.

## Tab containers (multiple charts shown one at a time, not simultaneously)
A native Qlik container that switches between several *full* charts via
tabs (as opposed to a KPI-tile grid container, which shows all its children
at once side-by-side) is a different Power BI equivalent: **separate report
pages**, not overlapping visuals on one page. If a container's children are
each substantial standalone charts/tables (not small uniform KPI tiles) and
the container's own properties indicate tabbed/single-visible-at-a-time
behavior (e.g. a `tabs`/`activeTab`-style property, or each child having its
own full-size bounds equal to the container's), emit one `page` per child
instead of packing them into one page's `visuals` array — name each page
after that child's own title so the tab structure survives as page tabs
across the bottom of the Power BI report, which is the closest native
equivalent to Qlik's in-sheet tabs.

# Field/measure binding (`query.queryState`)
The role name is **not interchangeable** — it must match the exact data
role the target `visualType` actually declares, or Power BI Desktop renders
that value slot as blank/empty with no visible error (this is a common,
silent cause of "the KPI/chart shows nothing"). By `visualType`:
- `clusteredColumnChart` / `clusteredBarChart` / `lineChart` /
  `lineClusteredColumnComboChart` / `scatterChart`: axis/category role →
  `"Category"`, values role → `"Y"` (scatter also uses `"X"`/`"Size"` when
  applicable).
- `card` / `multiRowCard`: values role → **`"Values"`** — not `"Y"`. A KPI
  card's single measure projects under `"Values"`, matching Power BI's
  built-in Card visual's own data role name.
- `pieChart` / `treemap`: category role → `"Category"`, values role →
  `"Y"`.
- `tableEx` / `pivotTable`: every column/measure shown projects under
  `"Values"` (or `"Rows"`/`"Columns"` for a pivot's row/column axes).
- `slicer`: the bound field projects under `"Values"`.
When in doubt for a visual type not listed here, match whichever of
`"Category"`/`"Y"`/`"Values"` the closest visually-similar type above uses
— don't default to `"Y"` for everything.
- A field projection:
  ```json
  {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "<Table>"}}, "Property": "<Field>"}}, "queryRef": "<Table>.<Field>"}
  ```
- A measure projection:
  ```json
  {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "<Table>"}}, "Property": "<MeasureName>"}}, "queryRef": "<Table>.<MeasureName>"}
  ```
- A hierarchy hop (drill-down dimension used on an axis) → project the
  hierarchy's top level column first; Power BI expands remaining levels at
  render/drill time, it does not need every level projected up front.

**`Property` is always a SIBLING of `Expression`, never nested inside it.**
This has been the single most common shape mistake — putting `Property`
next to `SourceRef` *inside* `Expression` instead of next to `Expression`
itself silently makes the whole field unresolvable (every reader, including
Power BI itself, looks for `Property` at the sibling position and finds
nothing there):
```json
// WRONG — Property nested inside Expression
{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "<Table>"}, "Property": "<Field>"}}}}
// RIGHT — Property is a sibling of Expression
{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "<Table>"}}, "Property": "<Field>"}}}
```

## Aggregating a raw field (count-distinct of a column, etc.)
When a projection needs an aggregate applied to a plain column that isn't
already a measure (e.g. "count distinct of InvoiceID" typed straight into a
KPI, not backed by a master measure), wrap the `Column` in a real PBIR
`Aggregation` node with an integer `Function` code — **never** add a
made-up sibling property like `"aggregate"`/`"aggregation"` next to
`field`. PBIR's schema has no such property anywhere on a projection; it
gets rejected outright ("An additional property '...' was included").
```json
{
  "field": {
    "Aggregation": {
      "Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "<Table>"}}, "Property": "<Field>"}},
      "Function": 2
    }
  },
  "queryRef": "<Table>.<Field>"
}
```
`Function` codes: `0`=Sum, `1`=Average, `2`=DistinctCount, `3`=Min, `4`=Max,
`5`=Count, `6`=Median, `7`=StandardDeviation, `8`=Variance.

## A KPI whose expression is a hard-coded constant (e.g. `=Sum(5)`)
This is a common Qlik idiom for a static target/placeholder tile — it isn't
a real aggregation over any field or measure. Do **not** invent a
fabricated table name (seen in practice: `"MeasureTable"`) to bind it to —
that table doesn't exist anywhere in the model and Power BI reports
"Fields that need to be fixed." Instead:
- Bind it as a normal `Measure` projection using the placeholder name as
  both `Entity` and `Property` is fine (a later deterministic build step
  recovers the real value and creates it as an actual measure) — but the
  numeric value MUST be recoverable from `"notes"` in one of these exact
  phrasings so that step can parse it: `"Placeholder measure '<Name>'
  (value=<N>)"` or `"...=Sum(<N>)..."`. Always include the literal number.

# KPI-specific rules
Qlik `kpi` objects usually carry one measure expression plus a label and a
conditional color/threshold. Map:
- The measure → a `card` visual's single `"Values"` projection (not `"Y"` —
  see the role-name table above; this is the single most common reason a
  converted KPI card renders blank in Power BI Desktop).
- Any threshold coloring (`layout.properties.color.conditions`) → the card's
  `objects.dataLabels` or `objects.background` conditional-formatting object
  (best-effort; note the raw Qlik condition in a comment field if it can't be
  expressed as a simple `IF` rule Power BI conditional formatting supports).

# Filters (listbox / filterpane objects)
Each become a `slicer` visual whose `visual.query.queryState.Values`
projects the bound field; do not create a page-level filter unless the Qlik
object was explicitly a global/sheet-level filter (`layout.properties.scope`).
