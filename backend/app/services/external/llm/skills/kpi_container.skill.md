---
name: qlik-kpi-container-to-cards
description: Convert a Qlik "KPI container" config table (one row per KPI tile, with a title, a measure reference, and colors) into individual Power BI KPI card visuals
---

# Role
Some Qlik apps don't build each KPI tile as its own native chart object.
Instead they use a **config table** — one row per KPI, with columns like
`Title`, `Measure`, `Bg Color` — read at runtime by a generic "KPI
container" extension that renders N tiles from N rows. The normal
sheet-object extraction never sees these as separate KPIs, since they're all
driven by one config table, not by individual chart objects. You convert
each row of that table into one real Power BI KPI card definition.

# Critical: preserve exact names
Every table name, field name, and measure name you write (`table` in
`synthesized_measure`, `measure_name`, any field reference inside a
synthesized DAX expression) must be **copied verbatim** from the config
table's own row data, the `measures` list, or `data_model` — same spelling,
same casing. Never invent a name, never normalize/clean one up, and never
substitute a name from this document's own illustrative examples (`Title`,
`Measure`, `Bg Color`, `SchemeName`, etc. are placeholders explaining the
*pattern*, not real names to fall back on). A renamed or invented
field/measure name breaks silently — the KPI card looks plausible right up
until Power BI can't find what it's bound to.

# Input you receive
One detected config table: `table` (name), `fields` (column names), `rows`
(every row as extracted, each a `{column_name: value}` dict) — plus the
app's real `measures` list (`measures.json`) and `variables` list
(`variables.json`) so you can resolve references.

# Output
Return one KPI definition per row that actually contains a title+measure
pair (skip rows where both are blank, and skip a row if `ShowCondition`-style
column evaluates to an always-false literal like `"0"`):
```json
{"kpis": [
  {
    "row_index": 0,
    "sheet": "<value of a Sheet/page-grouping column if present, else null>",
    "position": "<value of a KPI/ordinal column if present, else the row_index>",
    "title": "<title text>",
    "measure_name": "<real DAX measure name to bind, if directly resolvable>",
    "synthesized_measure": {"table": "<owning table — the table whose fields the expression aggregates>", "name": "...", "expression": "<DAX>", "format_string": "<optional>"},
    "subtitle_measure": {"table": "<owning table>", "name": "...", "expression": "<DAX>"},
    "bg_color": "<resolved hex/color if determinable, else null>",
    "confidence": "high",
    "notes": "<anything you couldn't convert deterministically>"
  }
]}
```
Include `measure_name` **or** `synthesized_measure`, not both. `subtitle_measure`
is independent of that choice — include it only if the row has a secondary
comparison-text column. `confidence` is `"high"`/`"medium"`/`"low"` — use
`"low"` for a synthesized expression you're not fully certain matches the
row's intended calculation, an unresolved cross-row title reference (the
circular-reference case above), or a color condition you couldn't resolve.

# Conversion rules

## Resolving the KPI's value expression
A config table's "measure" column holds a Qlik expression as a **string
value in the cell**, not a schema column — always resolve per-row:
- **Bare bracket reference**, the cell is exactly `[Measure Title]` → this
  names a real master measure. Look it up (case-insensitive) in the
  `measures` list; set `measure_name` to that measure's exact title. If no
  such master measure exists, treat it as the "ad-hoc" case below instead.
- **Any other expression** (contains `num(...)`, string concatenation `&`,
  `if(...)`, multiple bracket references, etc.) → this is itself a Qlik
  expression that needs full conversion, the same way `dax_measures.skill.md`
  converts a master measure — apply those same rules (set analysis →
  `CALCULATE`, `if` → `IF`/`SWITCH`, `num(x, fmt)` → drop the wrapper and put
  `fmt` in the KPI's own format string) to produce `synthesized_measure`.
  Bracket references inside the expression (`[Achievement %]`) become DAX
  measure references (`[Achievement %]`) unchanged — Power BI resolves a
  bracketed name in a measure expression to another measure the same way
  Qlik does.
- A secondary "second measure"/comparison-text column (e.g. showing "▲ vs
  100% target") is a separate small KPI card feature (subtitle text) — treat
  it exactly the same way (resolve or synthesize) and include it as a
  second `synthesized_measure` in a `"subtitle_measure"` field if present in
  the row; omit that field if the table has no such column.

## Resolving colors
- A literal hex/color name (`#22c3de`) → use as-is in `bg_color`.
- A Qlik variable reference (`$(vTeal)`) → look up `vTeal` in the `variables`
  list; if its `definition` is a literal color/hex, resolve it; if the
  variable's definition is itself computed/dynamic, leave `bg_color` null
  and explain in `notes` (Power BI conditional formatting for that card's
  background needs a manual rule referencing the same condition — this
  isn't something a static `bg_color` field can express).
- An `if(condition, colorA, colorB)` expression → this is genuinely
  conditional; leave `bg_color` null and put the resolved condition +
  color choices in `notes` so a person can wire it up as Power BI
  conditional formatting by hand.

## Grid columns
If the table has an explicit "Sheet"/page column and a "KPI"/position
column, copy their raw values into `sheet`/`position` — the builder uses
these to lay the cards out in the right grid order. If there's no such
column, use `row_index` for `position` and leave `sheet` null.

## A KPI row referencing another KPI row by title
A config table's expression cell can reference another row's *title*
directly, not a real master measure — e.g. a "Change vs Prior Period" KPI
row whose measure cell is `[SALES ACHIEVEMENT] - [Prior Period]` where both
`SALES ACHIEVEMENT` and `Prior Period` are themselves *other rows in this
same config table*, not entries in `measures.json`. Since every row's
measure needs its own `synthesized_measure` first before another row can
reference it, resolve in two passes rather than row-by-row top to bottom:
1. First pass: synthesize (or resolve to a real master measure) every row
   whose expression contains no bracket reference to another *config-table*
   title — only to real master measures or plain fields.
2. Second pass: for rows whose expression references another config-table
   title, substitute that title's own resolved measure name (from pass 1)
   as an ordinary DAX measure reference — same as referencing any other
   measure, since once pass 1 creates it, it's a real measure.
3. If two rows reference each other's titles (a genuine cycle — row A's
   expression names row B's title and vice versa), this can't be resolved
   in any order — don't invent an arbitrary DAX expression for either row;
   leave both `synthesized_measure` empty, set `"notes"` explaining the
   circular reference on both, and let a person untangle the intended
   calculation manually.

## ShowCondition
If a `ShowCondition`-style column is present and its value is a Qlik boolean
literal that's always false (`"0"`) for a row, omit that row entirely — it's
a KPI the Qlik author disabled. A value of `"1"` or a variable reference
means always show; include the row.
