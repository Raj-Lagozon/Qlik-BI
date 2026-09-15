---
name: qlik-dimensions-to-dax
description: Convert Qlik master dimensions and drill-down groups into DAX calculated columns and Power BI hierarchies
---

# Role
Convert each Qlik master dimension into either a plain column reference, a DAX
calculated column (when the dimension is an expression, not a bare field), or
a multi-level hierarchy (when the Qlik dimension is a drill-down group).

# Critical: preserve exact names
Every table name, column name, and dimension/hierarchy title you write must
be **copied verbatim** from the input JSON — same spelling, same casing.
Never invent a name, never normalize/clean one up, and never substitute a
name from this document's own illustrative examples (`Sales`, `Country`,
`Region`, `Geography`, etc. are placeholders explaining the *pattern*, not
real names to fall back on). A renamed or invented field/table name breaks
silently — the calculated column or hierarchy level looks plausible right
up until Power BI can't find the field it references.

# Input you receive
The full `dimensions` array from `dimensions.json` (`title`, `grouping` —
`"N"` for a single field/expression, `"H"` for a drill-down hierarchy —
`field_defs`, `field_labels`) plus `data_model` (tables/fields) so you can
resolve each dimension to its owning table.

# Output
Convert every item in the input array. Return:
```json
{"items": [
  {
    "type": "column" | "calculated_column" | "hierarchy",
    "table": "<owning table>",
    "name": "<title or level name>",
    "expression": "<DAX, only for calculated_column>",
    "levels": [{"name": "...", "column": "..."}],   // only for hierarchy
    "confidence": "high",
    "notes": "<optional>"
  }
]}
```
`confidence` is `"high"`/`"medium"`/`"low"` — your own judgment of this
translation's certainty. Use `"low"` for set-analysis-in-a-dimension you
had to guess the row/aggregate scope for, a ragged-hierarchy fallback
you're unsure matches the app's real data shape, or any nested `Aggr()`-like
construct without full context. Put the reason in `"notes"`.

# Conversion rules

## Simple field dimension (`grouping = "N"`, `field_defs` has 1 plain field)
Map straight to the existing column — no calculated column needed, just note
which table/column it points to so the report_visuals converter can reference
it directly (`Table[Column]`).

## Expression dimension (`grouping = "N"`, `field_defs[0]` is an expression like
`=If(Amount>1000,'High','Low')`)
Becomes a DAX calculated column on the owning table:
- `If(Amount>1000,'High','Low')` → `IF(Sales[Amount] > 1000, "High", "Low")`
- `Year(OrderDate)` → `YEAR(Sales[OrderDate])`
- `Month(OrderDate)` → `FORMAT(Sales[OrderDate], "MMM")` (or `MONTH()` if a
  numeric month is what's actually displayed — infer from `field_labels`)
- Nested/`Pick(Match(...))` bucket logic → `SWITCH(TRUE(), <cond1>, <val1>, <cond2>, <val2>, ..., <default>)`

## Drill-down hierarchy (`grouping = "H"`, multiple `field_defs`)
Emit one `hierarchy` output per dimension, with one `level` per field in
`field_defs`, in the same top-to-bottom order Qlik lists them (Qlik lists
drill-down fields from broadest to most granular, same convention Power BI
hierarchies use). Each level's `column` must already exist as a plain column
or calculated column on the table — if a level references an expression
field, first emit that as its own `calculated_column`, then reference it by
name in the hierarchy level.

Example — Qlik drill-down dimension `Geography` with fields
`[Country, Region, City]` → hierarchy:
```json
{
  "type": "hierarchy",
  "table": "Geography",
  "name": "Geography",
  "levels": [
    {"name": "Country", "column": "Country"},
    {"name": "Region", "column": "Region"},
    {"name": "City", "column": "City"}
  ]
}
```

## Set-analysis calculated dimension (not just plain field expressions)
An expression dimension isn't always a bare `If()`/`Year()` transform of one
field — it can itself contain set analysis, e.g.
`=Only({<Status={'Active'}>} CustomerTier)` (show the tier only for active
customers, blank otherwise) or `Count({<Region={'$(vRegion)'}>} OrderID)`
used AS a dimension (bucketing rows by a set-analysis-scoped count). Convert
the set-analysis portion the same way `dax_measures.skill.md` does (set
analysis → `CALCULATE` filter args), inside the calculated column's
row-context expression:
- `Only({<Status={'Active'}>} CustomerTier)` →
  `IF(Sales[Status] = "Active", Sales[CustomerTier], BLANK())`
  (a calculated column evaluates per-row, so a single-row-scoped set
  condition like this becomes a plain row-level `IF`, not a `CALCULATE` —
  `CALCULATE` inside a calculated column only makes sense when the set
  analysis is aggregating across rows, e.g. the `Count(...)` example below).
- `Count({<Region={'$(vRegion)'}>} OrderID)` used as a bucketing dimension →
  this aggregates across rows, so it needs `CALCULATE` inside the column
  expression, evaluated per row's group:
  `CALCULATE(COUNT(Sales[OrderID]), Sales[Region] = "West", ALLEXCEPT(Sales, Sales[CustomerID]))`
  (substitute the real grouping key for `CustomerID` and the real `vRegion`
  value, resolved from `variables.json`, for `"West"`).

## Unbalanced / ragged hierarchies
Qlik drill-down dimensions tolerate a row having no value for a deeper
level (e.g. a `Country → Region → City` hierarchy where some countries have
no `Region` data) — Power BI hierarchies don't natively skip blank levels,
they'll show a literal blank member at that level instead of collapsing up
to the parent. Handle it explicitly rather than emitting a hierarchy that
silently displays blank rows:
- For each level below the first, emit its calculated-column expression to
  fall back to the parent level's value when its own field is blank, so the
  hierarchy still reads as ragged/collapsed rather than showing empty nodes:
  `Region` level → plain column reference if always populated; if it can be
  blank, emit a calculated column instead:
  `IF(ISBLANK(Sales[Region]), Sales[Country] & " (no region)", Sales[Region])`.
- Note this fallback in `"description"` on that level's column entry so a
  person knows why a calculated column exists at a level that looks like it
  should've been a plain field reference.

## Naming and display
Use `title` from the master dimension as the hierarchy/calculated-column
display name so chart axis labels in Power BI match Qlik's dimension labels.
