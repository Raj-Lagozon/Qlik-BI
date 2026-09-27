---
name: qlik-sheets-to-powerbi
description: Convert Qlik master measures, master dimensions/hierarchies, sheet visuals, and KPI-container config tables into Power BI DAX measures, columns/hierarchies, and PBIR pages/visuals
---

# Role
You convert everything that renders on a Qlik **sheet**: master measures
(Task A), master dimensions/drill-downs (Task B), sheet visuals/charts/KPIs
(Task C), and KPI-container config tables (Task D). The caller tells you
which task a given request is via the payload's `task` field
(`"measures"`, `"dimensions"`, `"report"`, or `"kpi_container"`) — apply
only that task's rules below. Tasks A/B run before Task C so Task C can
reference already-converted measure/dimension/hierarchy names.

Reference: `qlik-bi-components/v1_powerbi-qlik-similarity-components.md`
§4 (Set Analysis), §5 (Alternate States), §7 (Measures), §8 (Qlik
Expression → DAX table), §9 (Dimensions/Master Items), §10 (Charts), §11
(KPI Container) document the full similarity rationale behind every rule
below.

# Critical: preserve exact names
Every table name, column name, measure title, and hierarchy/level name you
write must be **copied verbatim** from the input JSON — same spelling, same
casing, same punctuation. Never invent a name, never "clean up" or
normalize one, and never substitute a name from this document's own
illustrative examples (`Sales`, `Sales[Amount]`, `Country`, `Region`,
`<Table>`, `<Field>`, etc. are placeholders for explaining the *pattern* —
never real names to fall back on). A renamed or invented name breaks
silently — it looks plausible right up until Power BI shows "fields that
need to be fixed" or can't resolve a reference.

---

# Task A: Master Measures → DAX Measures

**If a Qlik measure aggregates a bare field name that appears NOWHERE in
the provided `data_model` (not a field on any table), do NOT invent a
table for it** — and in particular never emit the `FieldName[FieldName]`
shape (a fabricated table whose name equals the field's). That field is
genuinely missing from the extracted model — the source Qlik measure is
referencing data that wasn't loaded (an orphaned/broken master measure,
which Qlik apps do carry). Emit the measure with `"expression"` set to a
DAX comment naming the missing field, e.g.
`"BLANK() // TODO: Qlik referenced field 'ExpediteCost' which is not in the model"`,
`"confidence": "low"`, and the original Qlik expression quoted in
`"notes"`, so a person can wire it up by hand if that data is available in
Power BI. The measure must still exist (visuals bind to it by name) — it
just can't have a real formula.

**A reference to another measure is NEVER table-qualified — this is a
different rule from column qualification, and confusing the two is a
serious, common mistake.** A real *column* is written `Table[Column]`. A
reference to another *measure* (one already converted from a master
measure, or one you can see will be defined elsewhere in this same output)
is always written bare: `[Measure Name]` — no table name, no
apostrophe-quoted table prefix, ever. `Table[Measure Name]` is not
"more explicit" DAX — it's wrong DAX: it tells the engine to look for a
*column* literally named `Measure Name` on that table. If no such column
exists, this errors outright; if one coincidentally does, this silently
binds to the wrong thing (a column, not the intended aggregation) —
matching the exact "measure name used as a column name" failure this
mistake produces. When your expression needs to reference another measure
by name (e.g. `[Achievement %]` inside a bigger formula), always emit it
bare, regardless of which table logically "owns" that other measure.

## Cross-table row-by-row calculations (SUMX + RELATED)

A Qlik expression like `Sum( <per-row formula mixing fields from two
associated tables> )` becomes `SUMX(<table>, <per-row formula>)` in DAX —
but `<table>` and every field reference inside it must be chosen with the
relationship direction in mind, or Power BI fails with *"the column
'T[c]' either doesn't exist or doesn't have a relationship to any table
available in the current context."*

- **`RELATED(OtherTable[Col])` only traverses many → one.** It is valid
  *only* when the table you're iterating with `SUMX`/`AVERAGEX`/etc. is on
  the **many** side of a relationship to `OtherTable`, and `OtherTable` is
  on the **one** side. Iterating the one side and calling `RELATED()`
  toward the many side is the exact error above.
- **Iterate the table on the many side** — the fact/detail table, the one
  with more rows / the non-unique key. Its own columns are then referenced
  directly (no `RELATED`), and only the *one-side* table's columns go
  through `RELATED()`. Look at `data_model`: the table whose join key
  repeats is the many side; the table whose key is unique is the one side.
- **When the two tables share a key 1-to-1** (both keys unique — e.g. a
  fact table and a per-row "prediction"/"enrichment" table keyed on the
  same id), the relationship is one-to-one and `RELATED()` works in
  *either* direction, so iterate whichever table owns most of the per-row
  fields. If you cannot tell the direction from `data_model`, do **not**
  guess: use `LOOKUPVALUE(OtherTable[Col], OtherTable[Key], ThisTable[Key])`
  instead of `RELATED()` — it needs no relationship and is direction-
  agnostic (note this choice in `"notes"` and drop confidence).
- Never write `RELATED(SameTableYoureIterating[Col])` — reference that
  table's own columns bare as `Table[Col]`.

## Input you receive
The full `measures` array from `measures.json` (`title`, `expression`,
`label_expression`, `tags` per item) plus `data_model` (tables/fields) so you
can qualify fields correctly (`Table[Column]`) and decide which table should
own each converted measure (the table that owns the fields the expression
aggregates).

## Output
Convert every item in the input array. Return:
```json
{"measures": [
  {"table": "<owning table>", "name": "<title>", "expression": "<DAX>", "format_string": "<optional>", "description": "<optional>", "is_hidden": false, "confidence": "high", "notes": "<optional>"}
]}
```
`expression` is the DAX formula body only (no `measure X =` prefix — the
builder adds that). `confidence` is one of `"high"`, `"medium"`, `"low"` —
your own judgment of how certain this specific translation is, independent
of anything downstream code checks. Use `"low"` whenever you had to guess a
table/column name not present in the input, the Qlik construct has no clean
DAX equivalent (alternate states used as a formula, `Above()`/`Below()`
without real axis context, deeply nested `Aggr()`, etc.), or you produced a
best-effort expression you aren't confident reproduces the original
behavior exactly. Use `"medium"` for a reasonable but unverified assumption
(e.g. inferring a table qualifier from context rather than being told it
explicitly). Default to `"high"` for direct, unambiguous translations — do
not inflate confidence to avoid a low count; a real gap flagged here is
caught long before it becomes a wrong number on someone's dashboard. Put the
one-sentence reason for any non-`"high"` rating in `"notes"`.

## Conversion rules

### Bare variable substitution — `$(vVarName)`
This is different from every `$(=...)` form elsewhere in this document —
those contain an `=` and are Qlik *evaluating an expression* at reference
time. A bare `$(vVarName)` (no `=`) is simpler: it's pure text substitution
of whatever `vVarName`'s current value is, wherever it's written — often
directly in arithmetic, not inside set analysis at all:
```
($(vSalesAchievement%) / 100) * 12000
```
**Do not** resolve this by picking whatever existing measure looks
semantically closest to the variable's name (e.g. substituting a real
`Achievement %` measure just because the names sound similar) — a variable
converted elsewhere (by the script_conversion skill, possibly as an
interactive what-if slider) is a *specific, separate* artifact, not a
stand-in for the nearest similarly-named measure, and guessing wrong here
silently breaks the interactivity the app was built around (a manually-
adjustable slider becomes a fixed, non-reactive value with no visible
error). Instead, preserve the **exact original variable name** as a
bracket reference, unchanged: `$(vSalesAchievement%)` → `[vSalesAchievement%]`.
Resolving that name to whatever real DAX measure/parameter the variable
became is handled downstream by name-matching against the script_conversion
output — that's a deterministic lookup by the variable's own name, which
only works if you didn't substitute a different name in its place.
- `($(vSalesAchievement%) / 100) * 12000` → `([vSalesAchievement%] / 100) * 12000`
- `Sum(Amount) * $(vTaxRate)` → `SUM(Sales[Amount]) * [vTaxRate]`

### Basic aggregations
- `Sum(Amount)` → `SUM(Sales[Amount])`
- `Count(OrderID)` → `COUNT(Sales[OrderID])`
- `Count(DISTINCT CustomerID)` → `DISTINCTCOUNT(Sales[CustomerID])`
- `Avg(x)` / `Min(x)` / `Max(x)` → `AVERAGE` / `MIN` / `MAX`
- `Sum(Amount)/Count(OrderID)` → same division of `SUM`/`COUNT` measures.

### Set analysis → CALCULATE / filter context
Qlik set analysis `{<Field={'Value'}>}` inside an aggregation becomes a DAX
`CALCULATE` with a filter argument:
- `Sum({<Year={$(=Max(Year))}>} Amount)` →
  `CALCULATE(SUM(Sales[Amount]), Sales[Year] = CALCULATE(MAX(Sales[Year]), ALL(Sales[Year])))`
  or, cleaner, precompute the max year as its own measure/variable via `VAR`:
  ```
  VAR _MaxYear = CALCULATE(MAX(Sales[Year]), ALL(Sales[Year]))
  RETURN CALCULATE(SUM(Sales[Amount]), Sales[Year] = _MaxYear)
  ```
- `{<Field-={'X'}>}` (exclude) → `CALCULATE(<agg>, Sales[Field] <> "X")` or
  `CALCULATE(<agg>, NOT(Sales[Field] IN {"X"}))` for multi-value excludes.
- `{<Field={'A','B'}>}` (include list) → `CALCULATE(<agg>, Sales[Field] IN {"A","B"})`.
- `{<Field=>}` (empty set — **clear this one field's filter, keep every
  other current selection**; different from `{<Field-={'X'}>}` above, which
  excludes one specific value while still respecting a selection on that
  same field) → `CALCULATE(<agg>, ALL(Sales[Field]))`. `ALL()` on just that
  column removes any filter on it while leaving filters on every other
  column (from the visual's own context) untouched.
- `{1}` (ignore all selections, whole data set) → wrap with `ALL(...)` covering
  every table referenced by the base expression's filters:
  `CALCULATE(<agg>, ALL(Sales))` (or `ALL()` targeted at just the filtered
  tables if only some should be ignored).
- `{1<Region={"West"}>}` (fused: ignore all selections, THEN re-apply just
  one explicit filter) → `CALCULATE(<agg>, ALL(Sales), Sales[Region] = "West")`.
  List `ALL(Sales)` before the explicit re-added filter, matching the order
  Qlik's own syntax implies (full override first, selective re-add second) —
  DAX evaluates all `CALCULATE` filter arguments together regardless of
  order, but this ordering is the conventional idiom Power BI reviewers
  expect, so keep it even though it doesn't change the result.
- `{$}` (current selections, explicit) → no extra `CALCULATE` needed, the
  aggregation already respects the ambient filter context in DAX.
- Alternate states (`{<Field={1}>} SET1 as vSet1... TOTAL`) → model as
  separate measures referencing appropriately named `CALCULATE` filters;
  DAX doesn't have Qlik's named alternate-state concept — if the app truly
  needs two independent slicer states, flag this as needing 2 identical
  visuals bound to different report-level bookmarks/what-if filters rather
  than 1 DAX trick, and note it in `"description"`. (See also "Alternate
  states used directly in a formula" below.)

### Set-analysis modifier is a Qlik VARIABLE (`{<$(vSomeVar)>}`)
A real, confirmed bug class (user report, `Control_Tower_v2`): a set-analysis
modifier is sometimes not a literal condition but an entire Qlik **variable**
substituted in via dollar-sign expansion, e.g. `Avg({<$(vCurrMonthSet)>}
SafetyStockUnits)`. **Never convert this to a placeholder bracket reference**
like `CALCULATE(AVERAGE(...), [vCurrMonthSet])` — `vCurrMonthSet` is not a
DAX measure and never will be one, so `[vCurrMonthSet]` resolves to nothing
("The value for '...' cannot be determined") and — because Analysis Services
processes/recalculates measures transactionally across the WHOLE refresh —
ONE such broken measure fails every other table's refresh too, not just
this one. This has actually happened in production output; it is not a
theoretical risk.

Instead: **look up the variable's real definition** in the `variables`
extraction (same lookup `script_conversion.skill.md`'s "Variable holding a
full set-analysis expression" rule already documents for the load-script
side) and inline its ACTUAL resolved filter conditions directly into the
`CALCULATE` arguments, exactly as if the modifier had been written out
literally in the first place:
```
-- vCurrMonthSet's definition: ={<Year={2026}, Month={9}>}
-- Avg({<$(vCurrMonthSet)>}SafetyStockUnits)
--   → CALCULATE(AVERAGE(InventoryFact[SafetyStockUnits]), InventoryFact[Year] = 2026, InventoryFact[Month] = 9)
```
If the variable's definition genuinely cannot be resolved (script not
available, definition itself references something unresolvable), **omit
that filter argument entirely** rather than inventing a placeholder —
`CALCULATE(AVERAGE(InventoryFact[SafetyStockUnits]))` is a working measure
with a broader result; a bracket reference to nothing is not. Mark
`"confidence": "low"` and explain in `"notes"` which variable couldn't be
resolved and why, so a person can fix the filter by hand — never silently
guess and never leave an unresolvable bracket reference in the output DAX.

### Dynamic date-range idioms (MTD / QTD / YTD / rolling window)
A very common Qlik pattern builds BOTH bounds of a date filter from
`max(Date)` inside the set analysis string itself, via `$(=...)`
dollar-sign expansion:
```
{<Date={">=$(=floor(monthstart(max(Date)))) <=$(=max(Date))"}>}   -- MTD
{<Date={">=$(=floor(yearstart(max(Date)))) <=$(=max(Date))"}>}    -- YTD
{<Date={">=$(=floor(monthstart(max(Date),-1))) <=$(=floor(monthstart(max(Date)))-1)"}>}  -- prior month
{<Date={">=$(=floor(max(Date))-6) <=$(=max(Date))"}>}             -- trailing 7 days
```
This is a **range construction**, not a single-value substitution like the
`{<Year={$(=Max(Year))}>}` example above — both the lower and upper bound
derive from the same "latest date in scope" value, so compute that once and
reuse it:
```
VAR _MaxDate   = CALCULATE(MAX(Sales[Date]), ALLSELECTED(Sales))
VAR _RangeStart = EOMONTH(_MaxDate, -1) + 1        -- monthstart(max(Date))
RETURN
    CALCULATE(
        SUM(Sales[Amount]),
        Sales[Date] >= _RangeStart,
        Sales[Date] <= _MaxDate
    )
```

**Bound mapping** (substitute into `_RangeStart`, keep `_MaxDate` as the
upper bound unless the idiom itself shifts it too):
| Qlik bound expression | DAX |
|---|---|
| `monthstart(max(Date))` | `EOMONTH(_MaxDate, -1) + 1` |
| `monthstart(max(Date), -1)` (prior month start) | `EOMONTH(_MaxDate, -2) + 1` |
| `monthend(max(Date))` | `EOMONTH(_MaxDate, 0)` |
| `yearstart(max(Date))` | `DATE(YEAR(_MaxDate), 1, 1)` |
| `yearend(max(Date))` | `DATE(YEAR(_MaxDate), 12, 31)` |
| `quarterstart(max(Date))` | `DATE(YEAR(_MaxDate), (QUARTER(_MaxDate)-1)*3 + 1, 1)` |
| `quarterend(max(Date))` | `EOMONTH(DATE(YEAR(_MaxDate), QUARTER(_MaxDate)*3, 1), 0)` |
| `max(Date) - N` (rolling window) | `_MaxDate - N` |

**Filter-context rule for `_MaxDate` — get this right, it's the part that
silently breaks KPIs**: default to
`CALCULATE(MAX(Sales[Date]), ALLSELECTED(Sales))`, **not** plain
`MAX(Sales[Date])` and **not** `CALCULATE(MAX(Sales[Date]), ALL(Sales))`.
- Plain `MAX(Sales[Date])` is wrong because inside a visual sliced by
  `Sales[Date]` itself (a trend chart, a date-axis table), the ambient
  filter context already restricts `Date` to one row — "the latest date"
  would collapse to whatever date that row is, not the app's actual latest
  date, breaking the whole point of an "as of latest date" KPI.
- `ALLSELECTED(Sales)` is correct because it mirrors Qlik's `$(=...)`
  dollar-expansion semantics: that expression is evaluated once against
  *whatever the user currently has selected elsewhere* (region, product,
  etc.) but is never restricted by the `Date` clause being built inside the
  same set-analysis expression — `ALLSELECTED` reproduces exactly that
  (respects other slicers/filters, ignores the visual's own row/axis
  context and the date filter this measure is itself constructing).
- Plain `ALL(Sales)` is only correct when the *entire* Qlik set analysis
  wraps everything in `{1}` (e.g. `{1<Date={...}>}`), meaning the app author
  explicitly wanted the whole calculation to ignore every user selection,
  not just the date range being built. Don't default to `ALL` — check for
  that `{1}` wrapper first.

Whenever this idiom is detected, add a note in `"description"` flagging it
(e.g. `"MTD range measure — verify _MaxDate filter scope matches intent"`)
— getting `ALLSELECTED` vs `ALL` vs plain `MAX` wrong here is a common,
easy-to-miss source of a KPI silently going stale or collapsing to the
active slicer instead of showing "as of latest date" figures.

### TOTAL qualifier
Qlik's `TOTAL` keyword inside an aggregation is unrelated to set analysis
(`{1}`/`{$}`) — it strips chart dimensions from that one aggregation's
context. Get the scope right, it's easy to apply `ALL()` to the wrong thing:
- **Bare `TOTAL`, no field list** (`Count(TOTAL ProductID)`,
  `Sum(TOTAL Amount)`) → ignore *every* dimension currently grouping the
  visual (a true grand total) — `ALL()` the whole fact table, not the column
  being aggregated:
  `CALCULATE(COUNT(Sales[ProductID]), ALL(Sales))`.
  Getting this wrong (e.g. `ALL(Sales[ProductID])` instead of `ALL(Sales)`)
  is a common mistake — when the aggregated column isn't the same as the
  visual's actual grouping dimension, `ALL()` on the aggregated column alone
  is a no-op and silently makes the ratio always evaluate to the same value
  as its numerator (e.g. 100%). If you don't know the visual's real grouping
  dimension(s) from context, `ALL()` on the whole table is the safe default
  for a bare `TOTAL`.
- **`TOTAL <Dim1, Dim2>`** (explicit field list) → ignore only those named
  dimensions, keep every other filter active:
  `Count(TOTAL <Region> Amount)` → `CALCULATE(SUM(Sales[Amount]), ALL(Sales[Region]))`.
- Contrast with `{1}` set analysis (ignores *selections*, i.e. user filter
  context) — `TOTAL` ignores *chart dimensions* (the visual's own grouping).
  Both often end up as `ALL(...)` in DAX, but reason about which one the
  Qlik expression actually uses before picking the `ALL()` target.

### RangeSum / accumulation
- `RangeSum(Above(Sum(Amount), 0, RowNo()))` (running total) →
  `CALCULATE(SUM(Sales[Amount]), FILTER(ALL(Sales[Date]), Sales[Date] <= MAX(Sales[Date])))`
  (adjust the axis field to whatever the visual's running dimension is).
- `Above(Sum(Amount), N, 1)` (value N rows above the current one, not a running
  sum) → needs the visual's actual axis field to rank against:
  `VAR _CurrentRank = RANKX(ALLSELECTED(Sales[Month]), CALCULATE(SUM(Sales[Amount])), , DESC, Dense)`
  `RETURN CALCULATE(SUM(Sales[Amount]), FILTER(ALLSELECTED(Sales[Month]), RANKX(ALLSELECTED(Sales[Month]), CALCULATE(SUM(Sales[Amount])), , DESC, Dense) = _CurrentRank - N))`
  — this needs the real axis field substituted for `Sales[Month]`; if it
  isn't discoverable from context, use `"notes"` to flag that the offset
  axis must be confirmed rather than guessing the wrong field.
- `Below(...)` is the same pattern as `Above` with the rank offset sign
  flipped (`_CurrentRank + N` instead of `_CurrentRank - N`).

### Rank() — standalone ranking
`Rank(Sum(Sales))` (not wrapped in `Above`/`Below`) ranks the current row
against every other value of whatever dimension the chart is grouped by —
it isn't independently translatable without knowing that partition
dimension, the same limitation as `Above`/`Below` above:
```
RANKX(ALLSELECTED(Sales[Region]), CALCULATE(SUM(Sales[Amount])), , DESC, Dense)
```
Substitute the real axis/grouping field for `Sales[Region]` (from the
chart's actual dimension, not guessed) and reference an existing measure
(`[Total Sales]`) instead of re-deriving the aggregation inline when one is
already defined for that same expression. If the grouping dimension isn't
discoverable from the input, produce your best-effort guess, set
`"confidence": "low"`, and say exactly which dimension you assumed in
`"notes"` — Qlik's `Rank()` infers its partition from chart context
implicitly, so this is exactly the kind of assumption that needs human
review rather than silent approximation.

### Aggr() — chart-level aggregation
`Aggr(expression, Dim1, Dim2, ...)` computes `expression` once per
`Dim1`×`Dim2` combination, producing a virtual table Qlik then
aggregates/ranks over — this is DAX's `SUMMARIZE`/`ADDCOLUMNS` +
outer-aggregation pattern, not a single `CALCULATE`:
- `Sum(Aggr(Sum(Amount), CustomerID))` (re-aggregate a per-customer total —
  common no-op pattern, just equals `Sum(Amount)`) →
  `SUM(Sales[Amount])`.
- `Avg(Aggr(Sum(Amount), CustomerID))` (average of per-customer totals — a
  true re-aggregation) →
  ```
  AVERAGEX(
      SUMMARIZE(Sales, Sales[CustomerID]),
      CALCULATE(SUM(Sales[Amount]))
  )
  ```
- `Count(Aggr(Sum(Amount), CustomerID))` (count customers with a total, often
  paired with a threshold) →
  ```
  COUNTROWS(
      FILTER(
          SUMMARIZE(Sales, Sales[CustomerID]),
          CALCULATE(SUM(Sales[Amount])) > 0
      )
  )
  ```
- `Avg(Aggr(Sum(Sales), Customer, Month))` (two grouping dimensions) →
  `AVERAGEX(SUMMARIZE(Sales, Sales[CustomerID], 'Date'[Month]), CALCULATE(SUM(Sales[Amount])))`.
- Nested `Aggr(Aggr(...))` (aggregate-of-an-aggregate, e.g. rank-of-ranks) →
  nest the same `SUMMARIZE`/`ADDCOLUMNS` pattern one level deeper; if the
  nesting is more than 2 levels deep, note it in `"description"` rather than
  producing an unverifiable wall of nested `ADDCOLUMNS` — this is a case
  worth a person double-checking against the Qlik original.
- `FirstSortedValue(DimField, Aggr(AggExpr, DimField))` — Qlik's "return the
  dimension value that had the single BEST aggregated result" idiom (e.g.
  `FirstSortedValue(RepName, Aggr(Sum(Sales), RepName))`, sorted descending
  by default = "top earner's name"). This is NOT plain `Aggr()`
  re-aggregation (nothing above computes it) — it needs the row itself, not
  just a number, so translate as `TOPN` over the per-dimension virtual table
  picking exactly 1 row, then pull the dimension value out of that table:
  ```
  CALCULATE(
      SELECTEDVALUE(Sales[RepName]),
      TOPN(
          1,
          SUMMARIZE(Sales, Sales[RepName]),
          CALCULATE(SUM(Sales[SalesAmount])), DESC
      )
  )
  ```
  A LEADING `-` on the `Aggr(...)` expression (`FirstSortedValue(RepName,
  -Aggr(Sum(Sales), RepName))`) reverses Qlik's default sort direction —
  ascending instead of descending (i.e. "the WORST/smallest", not the
  best) — flip `DESC` to `ASC` in that case, don't just drop the `-`.

### Alternate states used directly in a formula
Qlik alternate states (`Set1`, defined via the sheet's "Alternate States"
panel, then referenced as `Sum({Set1} Amount)` inside an actual measure
expression — not just the "two independent slicer states" case already
covered above) don't have a DAX equivalent filter context, because a DAX
measure only ever sees one (the report's) filter context at evaluation
time. There is no clean built-in workaround: convert by picking the
concrete, named condition that state represents in the app (check the
sheet's alternate-state field selections in context) and inlining it as an
ordinary `CALCULATE` filter — e.g. if `Set1` is documented/observed as
"Region = West": `Sum({Set1} Amount)` →
`CALCULATE(SUM(Sales[Amount]), Sales[Region] = "West")`. If the state's
actual condition can't be determined from the extracted data, don't guess —
emit `CALCULATE(SUM(Sales[Amount]))` (current context, the closest safe
default) and flag it clearly in `"description"` as needing manual review
(possible workarounds beyond inlining: a disconnected comparison table,
Calculation Groups, or a duplicated dimension table per state — these are
structural model changes outside a single measure's scope and should be
flagged for the data-model task/a person, not improvised here).

### P() / E() set functions
`P(Field)` (possible values given current selections) and `E(Field)`
(excluded values) appear almost always inside a `Count`/`Sum` set-analysis
clause like `Count({<CustomerID = P(CustomerID)>} CustomerID)` — that
pattern just means "current selection state for this field," which is
already DAX's default ambient filter context:
- `Count({<CustomerID = P(CustomerID)>} CustomerID)` → `DISTINCTCOUNT(Sales[CustomerID])`
  (the `P()` round-trip is a no-op — it doesn't change what's selected).
- `Count({<CustomerID = E(CustomerID)>} CustomerID)` (rows NOT in the
  current selection) →
  `CALCULATE(DISTINCTCOUNT(Sales[CustomerID]), EXCEPT(ALL(Sales[CustomerID]), VALUES(Sales[CustomerID])))`.

### IF / conditional logic
- `If(condition, true_expr, false_expr)` → `IF(<condition>, <true_expr>, <false_expr>)`, translating Qlik boolean operators (`=`, `<>`, `and`, `or`) 1:1 (DAX uses the same comparison operators; `and`/`or` map to `&&`/`||` or `AND()`/`OR()`).
- See the Qlik Expression → DAX reference table at the end of this document
  for general-purpose functions (`Pick`, `Match`, `Alt`, date/text
  functions) beyond aggregation/set-analysis.

### Formatting
Carry over Qlik's number format (from the measure's label/format tag if
present) into `format_string` using DAX/Power BI format-string syntax, e.g.
Qlik `#,##0.00` stays `#,##0.00`; Qlik `$#,##0;-$#,##0` → `"$"#,##0;-"$"#,##0`.
**Never** convert a Qlik `Num(...)`-style formatting call into a DAX
`FORMAT()` wrapper around the measure's own value — `FORMAT()` converts the
result to TEXT, breaking numeric aggregation/sorting/conditional formatting
everywhere else that measure is used. Keep the measure numeric; put the
formatting only in `format_string`.

### Naming
Keep the same measure `title` as the DAX measure name so visuals map 1:1.

---

# Task B: Master Dimensions → Columns / Hierarchies

## Input you receive
The full `dimensions` array from `dimensions.json` (`title`, `grouping` —
`"N"` for a single field/expression, `"H"` for a drill-down hierarchy —
`field_defs`, `field_labels`) plus `data_model` (tables/fields) so you can
resolve each dimension to its owning table.

## Output
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

## Conversion rules

### Simple field dimension (`grouping = "N"`, `field_defs` has 1 plain field)
Map straight to the existing column — no calculated column needed, just note
which table/column it points to so Task C can reference it directly
(`Table[Column]`).

### Expression dimension (`grouping = "N"`, `field_defs[0]` is an expression like
`=If(Amount>1000,'High','Low')`)
Becomes a DAX calculated column on the owning table (see also the
calculated-column-vs-measure decision tree referenced in the appendix — a
per-row expression like this belongs to the column side, not a measure):
- `If(Amount>1000,'High','Low')` → `IF(Sales[Amount] > 1000, "High", "Low")`
- `Year(OrderDate)` → `YEAR(Sales[OrderDate])`
- `Month(OrderDate)` → `FORMAT(Sales[OrderDate], "MMM")` (or `MONTH()` if a
  numeric month is what's actually displayed — infer from `field_labels`)
- Nested/`Pick(Match(...))` bucket logic → `SWITCH(TRUE(), <cond1>, <val1>, <cond2>, <val2>, ..., <default>)`

### Drill-down hierarchy (`grouping = "H"`, multiple `field_defs`)
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

### Set-analysis calculated dimension (not just plain field expressions)
An expression dimension isn't always a bare `If()`/`Year()` transform of one
field — it can itself contain set analysis, e.g.
`=Only({<Status={'Active'}>} CustomerTier)` (show the tier only for active
customers, blank otherwise) or `Count({<Region={'$(vRegion)'}>} OrderID)`
used AS a dimension (bucketing rows by a set-analysis-scoped count). Convert
the set-analysis portion the same way Task A does (set analysis →
`CALCULATE` filter args), inside the calculated column's row-context
expression:
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

### Unbalanced / ragged hierarchies
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

### Master Items — broader umbrella
Qlik's "Master Item" concept is broader than just Master Dimensions/
Measures — it also includes reusable **Master Visualizations** (a whole
saved chart definition, reusable across sheets). Power BI has no equivalent
reusable "master visualization" object; a Qlik Master Visualization must be
re-created per-visual on each Power BI page it's used on (handled in Task C)
rather than referenced as one shared definition — note this if the input
data indicates a dimension/measure is also backing a saved Master
Visualization.

### Naming and display
Use `title` from the master dimension as the hierarchy/calculated-column
display name so chart axis labels in Power BI match Qlik's dimension labels.

---

# Task C: Sheets → PBIR Pages / Visuals

**Never truncate or summarize the visuals array — every object in the
input must produce a real, complete visual entry, with no exceptions for
a long or repetitive batch (e.g. a container with many near-identical
small tiles).** A real, confirmed bug: a batch response once ended with a
placeholder entry instead of fully enumerating every visual —
`{"name": "... (additional KPI and button visuals omitted for
brevity) ..."}`, with no `visual` key at all. That entry has no
`visualType`, which is a REQUIRED PBIR property — Power BI Desktop refused
to open the WHOLE report over it ("Required property 'visualType' was not
included in the /visual property"), not just that one object. If a batch
is large, still convert every single object individually and completely —
never emit a summarizing sentence, an ellipsis, or a "see above" shorthand
in place of a real visual entry.

## Input you receive
One entry from `sheets.json` (`id`, `title`, `objects[]` — each with `type`,
`bounds {x,y,width,height}`, `layout.properties`, `layout.layout`), the DAX
measure names and dimension/hierarchy names already produced by Task A/B (so
you reference the *converted* names, not the raw Qlik expressions), **and
the full `data_model`** (tables/fields) — required whenever an object binds
a PLAIN FIELD directly rather than a master measure/dimension (e.g. a
straight table's raw columns like `CustomerName`/`Open Dispute Value` —
common, not an edge case). Resolve those against `data_model`'s real
table/field names the same way you'd resolve a master measure's owning
table; **never leave a projection's `field` empty or omit it because the
right table "wasn't provided"** — data_model is always given precisely so
this doesn't happen. If a field genuinely isn't found anywhere in
`data_model` after checking every table, that's the one case to leave it
unresolved (and say so in `"notes"`) — not a first resort.

## Output — one JSON object per sheet
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
`page.json`/`visual.json` shape — the builder writes it out mostly as-is,
only filling in boilerplate (`$schema`, `annotations`, GUID-safe names).

## Title, subtitle, and axis title/label visibility — mirror Qlik's own settings, never Power BI's defaults
Every Qlik object's `layout.properties` carries its OWN configured
`title`/`subtitle`/`footnote` text and a `showTitles` flag — **always use
this exact text**, never a generic auto-generated name like "Sum of
OutstandingAmount by AgingBucket" (Power BI's own default when no title is
set). A real review of a converted app found titles/subtitles missing or
replaced with auto-generated names on nearly every chart — this is the
single most common visual-fidelity gap, so treat it as required, not
optional, whenever `layout.properties.title`/`subtitle` is non-empty.

Set title/subtitle as `visualContainerObjects` properties — a
CONTAINER-level property (same category as `action`/border/background
covered above), NESTED INSIDE `visual` itself (a sibling of `query`/
`objects`), never as a top-level key of the visual.json file:
```json
{
  "visual": {
    "visualType": "clusteredColumnChart", "query": {...}, "objects": {},
    "visualContainerObjects": {
      "title": [{"properties": {
        "text": {"expr": {"Literal": {"Value": "'AR Aging Distribution'"}}},
        "show": {"expr": {"Literal": {"Value": "true"}}}
      }}],
      "subTitle": [{"properties": {
        "text": {"expr": {"Literal": {"Value": "'Outstanding amount by aging bucket'"}}},
        "show": {"expr": {"Literal": {"Value": "true"}}}
      }}]
    }
  }
}
```
Putting `visualContainerObjects` as a top-level sibling of `visual` (one
level too shallow) is invalid PBIR ("An additional property
'visualContainerObjects' was included in the root property of
visuals/.../visual.json") and rejects the WHOLE report, not just that one
visual — always nest it inside `visual` as shown above.

**There is no `footer` property anywhere in PBIR — never emit one.** A Qlik
KPI's `footnote` text (e.g. "Across 20 active accounts") has no dedicated
container-level slot in Power BI; `title`/`subTitle` are the ONLY two
text-bearing groups that exist. When a Qlik object has footnote text, fold
it into `subTitle` instead: if `subtitle` is empty, use the footnote text as
the subtitle outright; if `subtitle` is already set, append the footnote
text after it (e.g. `'Outstanding amount by aging bucket · Across 20 active
accounts'`) — never invent a `footer`/`footnote` key under
`visualContainerObjects`, Power BI Desktop rejects the WHOLE report over it
the same way it does for `action`/`buttonText` ("An additional property
'footer' was included in the /visual/visualContainerObjects property").
Note the DAX-literal-string quoting inside `Value` — a plain unquoted
string here is invalid (Value must itself be a valid DAX expression text,
and `"AR Aging Distribution"` alone isn't one; `'AR Aging Distribution'`
— a single-quoted string literal — is). If `showTitles` is `false` in the
Qlik object, set `"show"`'s `Value` to `"false"` instead of omitting the
title block entirely, so an explicit "hidden" choice is preserved rather
than left to Power BI's own default (which shows a title by default —
the opposite of what an explicitly-hidden Qlik title means).

**Axis titles and labels**: a Qlik chart's `dimensionAxis.show`/
`measureAxis.show` property controls this per axis independently — values
are `"none"` (nothing shown), `"labels"` (tick labels only, no axis
title), or `"title-and-labels"`/similar (both). Reproduce this exactly via
`visual.objects.categoryAxis`/`valueAxis` `showAxisTitle`/`show`
properties — do NOT let Power BI's own chart-type default decide whether
an axis title appears; a real review found axis titles appearing in Power
BI that Qlik never showed (`measureAxis.show: "labels"`, no title, PBI
showed one anyway), and the reverse (Qlik showing a title, Power BI
defaulting to none). When genuinely unsure of the exact PBIR property
path for a specific chart type's axis object, still record Qlik's own
`show` value in `"notes"` so a person can apply it by hand rather than
silently falling back to Power BI's default either way.

## Layout: Qlik grid → Power BI canvas pixels
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

## Visual type mapping (Qlik object `type` → Power BI `visualType`)
| Qlik | Power BI |
|---|---|
| `barchart` (vertical) | `clusteredColumnChart` (or `columnChart` for stacked — check `layout.properties.stacked`) |
| `barchart` (horizontal) | `clusteredBarChart` |
| `linechart` | `lineChart` |
| `combochart`, 2+ measures (a genuine bar+line combination) | `lineClusteredColumnComboChart` |
| `combochart`, exactly 1 measure (no line series at all — Qlik's combo object used purely as a bar/column chart, a common authoring choice even with nothing to "combo") | same orientation-aware `clusteredColumnChart`/`clusteredBarChart` choice as plain `barchart` above — never force it into the column-only combo visual type just because Qlik's own object type says "combochart" |
| `piechart` | `pieChart` |
| `treemap` | `treemap` |
| `scatterplot` | `scatterChart` |
| `kpi` | `card` (single measure) or `multiRowCard` (multiple measures) |
| `table` | `tableEx` |
| `pivot-table` | `pivotTable` |
| `gauge` | `gauge` |
| `bulletchart` (native Qlik KPI-vs-target bar with bands) | approximate as `gauge` (measure = value, `layout.properties.targetValue`/similar → gauge target/max) if a true bullet visual isn't available — do NOT drop the object or fall back to a placeholder textbox just because there's no 1:1 Power BI equivalent |
| `map` (area/point) | `map` or `filledMap` depending on `layout.properties.mapType` |
| `listbox` | `slicer` (list style) |
| `filterpane` | one `slicer` per contained field |
| `sn-slider` (native Qlik numeric slider, usually bound to a variable via `layout.properties.qHyperCubeDef` or a direct variable input) | `slicer` in "Between"/numeric range style, bound to the slider's own field, OR — if the slider is really controlling a variable, not filtering a field — treat it the same as an action-button `setVariable` scenario: a disconnected scenario table + slicer (see the KPI-container/variable-scenario handling elsewhere in this skill), never a plain unmapped/placeholder object |
| `sn-grid-chart` (native Qlik grid/matrix chart — dimensions down the rows, measures across columns, like a lightweight pivot) | `tableEx` if it has one dimension, `pivotTable` if it has more than one row dimension or any column dimension — same shape reasoning as `table`/`pivot-table` above |
| `text-image` | `textbox` |
| `sn-table` (native Sense table) | `tableEx` |
| `button` (selection/variable-change/navigation/open-URL/trigger action) | **drop the object entirely — do not emit an `actionButton` visual for it.** Button/action visuals are not reproduced by this pipeline; omit the object from the output rather than emitting a button (with or without a real action). |
| Show/Hide or Enable condition on any object | not a visual type — see "Conditional visibility" below |

**Determining orientation for `barchart`/`combochart`**: read
`layout.properties.orientation` on the object itself — it is always
either `"horizontal"` or `"vertical"` (Qlik's own explicit authoring
choice, never inferred). `"horizontal"` → `clusteredBarChart`;
`"vertical"` (or the property absent, which defaults to vertical in
Qlik) → `clusteredColumnChart`. **Do not default to vertical without
checking this property** — a real review found a Qlik `combochart`
explicitly set to `orientation: "horizontal"` converted into a vertical
`lineClusteredColumnComboChart` anyway, because nothing checked the
property at all. This applies to `combochart` too whenever it degrades
to the plain bar/column case above (1 measure, no real combo).

**Buttons are dropped, not converted (see the `button` row in the visual
type mapping table above) — do not emit an `actionButton` visual, and do
not emit an `action`/`buttonText` key on any visual, ever.** An earlier
version of this pipeline did convert Qlik `button` objects into
`actionButton` visuals; that support has been removed because it produced
unwanted button/blank-textbox clutter on the report, so simply omit the
object from the output instead. (`action`/`buttonText` were never real
PBIR properties anyway — there is no schema property for either under
`visual` or `visualContainerObjects`; Power BI's real per-visual navigation
property is `visualLink`, which this pipeline no longer generates at all.)

**`confidence` and `notes` are top-level siblings of `visual` — one level
up from it, on the SAME visual entry, never keys inside `visual` itself —
and `objects` is a sibling of `query` inside `visual`, never nested one
level deeper INSIDE `query`.** These are two of the most common shape
mistakes in this task's output, both invalid PBIR for the exact same
reason `action`-inside-`visual` is above (`visual`'s only allowed keys are
`visualType`/`query`/`objects`; `query`'s only allowed key is
`queryState`):
```json
// WRONG — confidence/notes inside visual, objects nested inside query
{
  "name": "<objectId>",
  "visual": {
    "visualType": "card",
    "query": {"queryState": {...}, "objects": {}},
    "confidence": "high",
    "notes": "..."
  }
}
// RIGHT — confidence/notes beside visual, objects beside query
{
  "name": "<objectId>",
  "visual": {
    "visualType": "card",
    "query": {"queryState": {...}},
    "objects": {}
  },
  "confidence": "high",
  "notes": "..."
}
```

If a Qlik object type has no reasonable Power BI equivalent (e.g. a custom
extension object) **and it has no `qHyperCubeDef` of its own** (no real
dimensions/measures bound to it — a pure decorative/config extension),
**drop the object entirely.** Do not emit a placeholder/explanatory
`textbox` in its place — an empty or note-only textbox left on the page is
unwanted clutter, not a useful stand-in. Note the dropped object (id, title,
type) in `"notes"` on the page-level response instead, so it's traceable
without adding anything visible to the report.

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

**A textbox/label whose Qlik source is a DYNAMIC expression with no Power BI
equivalent (most commonly `ReloadTime()` in a "Last reload: ..." label, or
any other `=`-expression referencing something the model doesn't have) must
NEVER have an angle-bracket or otherwise template-looking placeholder
written into its actual visible `textRuns[].value`.** A value like
`"Last reload: <dynamic timestamp>"` reads as broken/unfinished UI to the
end user and must not be produced, even with a `"notes"` explanation
attached — the explanation belongs ONLY in `"notes"`, never inlined into
the rendered text itself. Instead:
- If the label has a static prefix (e.g. `"Last reload: "` before the
  dynamic part), keep just that static text and drop the unresolvable
  dynamic suffix entirely (`"Last reload:"`), OR
- If the entire label is dynamic with no usable static portion, **drop the
  textbox visual entirely** rather than emitting one with blank/empty text —
  an empty textbox is still unwanted clutter on the page.
Either way (including the drop case), put the real explanation and the
original Qlik expression in `"notes"` (e.g. `"Original label used
ReloadTime(), which has no Power BI equivalent — see the similarity doc
§13; object dropped"`) so a person knows why, without leaving a blank box
in the report itself.

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

A KPI-tile grid container (all children shown at once, side-by-side) has NO
true Power BI container equivalent at all — its real children (fetched via
`GetChildInfos`) must be reproduced as independent PBIR visuals
auto-laid-out within the container's original bounds; there is no single
PBIR "container" object to place them inside.

## Conditional visibility (Show/Hide, Enable conditions)
Qlik's declarative show/hide expression on an object has no single Power BI
property equivalent — reconstruct it using whichever of these actually
matches the source app's intent: a bookmark toggling visibility, a
visual-level filter, a measure the visual's own visibility is conditioned
on, or the report's Selection pane per-visual visibility. This is not a
direct field-to-field conversion; note in `"notes"` which mechanism you
chose and why.

## Field/measure binding (`query.queryState`)
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
- `pieChart`: category role → `"Category"`, values role → `"Y"`.
- **`treemap` uses COMPLETELY DIFFERENT role names from every other chart
  above — `"Group"` / `"Details"` / `"Values"`, never `"Category"`/`"Y"`.**
  Confirmed in practice: a treemap converted with `"Category"`/`"Y"` (the
  pieChart-style roles, an easy mistake since both are "categorical size"
  charts) shows as a completely empty visual with NOTHING in either the
  Category or Values well in Power BI Desktop's own Fields pane — not a
  silent-blank-value case like the KPI role mistake above, but a fully
  unrecognized role name that Power BI's Treemap visual has no well for at
  all, so the data is invisible in the UI even though the JSON technically
  carries it under the wrong key.
  - One dimension only → its single projection goes under `"Group"`.
  - A Qlik treemap with a two-level hierarchy (the common case — e.g.
    Category → Brand) → the FIRST/outer dimension's projection goes under
    `"Group"`, the SECOND/inner dimension's projection goes under
    `"Details"` (its own separate role, not a second projection inside
    `"Group"`).
  - The size measure → `"Values"` (not `"Y"`).
  ```json
  "queryState": {
    "Group": {"projections": [{"field": {...}, "queryRef": "<Table>.<OuterDim>"}]},
    "Details": {"projections": [{"field": {...}, "queryRef": "<Table>.<InnerDim>"}]},
    "Values": {"projections": [{"field": {...}, "queryRef": "<Table>.<Measure>"}]}
  }
  ```
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

## A field/dimension/measure with no usable label at all — NEVER invent a name
A chart object's `qFieldLabels`/`qLabel` can be **present but blank**
(`[""]`) — not missing, an actual empty string — most often on a dimension
built from a `&`-concatenation or other expression with no explicit label
set (`=CustomerID & ' - ' & CustomerName`, label `[""]`). This is
different from "no label field at all"; check for blank/empty specifically,
not just absence.

**You must NEVER invent a plausible-sounding, human-readable name for the
field/column/measure in this case** (seen in practice: binding to
`CustomerDim[Customer Label]`, a name that exists nowhere — not in
`data_model`, not in `measures`/`dimensions`, not anywhere — with a
`"notes"` claim that it was "a calculated column created in Task B"). **You
do not create columns or measures. Only Task A/B (of this skill) or the
ad-hoc-expression pass do that — a fabricated name has zero chance of
matching whatever name THOSE separate passes independently give the same
expression**, since they're working from the same raw expression text but
have no visibility into whatever name you invented here. This is a
guaranteed "fields that need to be fixed" error, and unlike most binding
mistakes, no downstream alias-matching pass can rescue it — a made-up
English phrase has no relationship to the real expression at all.

**The fix**: bind `Property` to the RAW QLIK EXPRESSION TEXT itself
(exactly as it appears in `qFieldDefs`/the measure's `qDef`, including the
leading `=` if present) instead of any name — e.g. `Property:
"=CustomerID & ' - ' & CustomerName"`, `queryRef:
"<Table>.=CustomerID & ' - ' & CustomerName"`. This looks unusual but is
deliberate and already relied upon elsewhere in this pipeline: the ad-hoc
measure/dimension conversion pass resolves a chart's derived
fields/measures from the SAME raw expression text (carried as
`qlik_source`) and creates the real calculated column/measure under
whatever name IT derives — a later build step then matches this binding
back to that real column purely by the shared raw-expression-text alias,
regardless of what either side calls it. Binding to the raw text is what
makes that match possible; binding to an invented name breaks it
permanently. Set `"confidence": "low"` and note in `"notes"` that this
field has no label and is bound by raw expression text, pending the
ad-hoc pass resolving it to a real column/measure — do NOT claim in
`"notes"` that any column or measure was created; only report the gap.

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

## KPI-specific rules
Qlik `kpi` objects usually carry one measure expression plus a label and a
conditional color/threshold. Map:
- A single-measure KPI → a `card` visual's single `"Values"` projection (not
  `"Y"` — see the role-name table above; this is the single most common
  reason a converted KPI card renders blank in Power BI Desktop).
- Any threshold coloring (`layout.properties.color.conditions`) → the card's
  `objects.dataLabels` or `objects.background` conditional-formatting object
  (best-effort; note the raw Qlik condition in a comment field if it can't be
  expressed as a simple `IF` rule Power BI conditional formatting supports).

**A KPI with a primary value AND a secondary/comparison value (`qMeasures`
has 2 entries — e.g. a big "Net Sales: 547.5k" number with a smaller
"vs Target -87.27%" line underneath, in the SAME Qlik tile) MUST become ONE
Power BI visual, not two.** Splitting the two measures into separate Card
visuals — even positioned right next to each other — is wrong: it breaks
the "one KPI, one glance" grouping the original app deliberately designed,
each half ends up sized/labeled like a full independent KPI instead of a
primary+secondary pair, and it's the direct cause of a converted dashboard
looking like it has 2x as many disconnected tiles as the source app. Use
`multiRowCard` (not `card`) for a 2-measure KPI, with BOTH measures
projected under the SAME `"Values"` array, primary measure first:
```json
"visual": {
  "visualType": "multiRowCard",
  "query": {"queryState": {"Values": {"projections": [
    {"field": {"Measure": {...}}, "queryRef": "<Table>.<PrimaryMeasure>"},
    {"field": {"Measure": {...}}, "queryRef": "<Table>.<SecondaryMeasure>"}
  ]}}},
  "objects": {}
}
```
One `position`/bounds entry for the WHOLE tile (the Qlik object's own
bounds, not split into two half-width boxes). If the secondary measure
needs its own ad-hoc DAX conversion (not a real master measure), it still
gets synthesized the normal way (see the ad-hoc-expression handling
elsewhere in this document) — the fix here is only about which VISUAL that
synthesized measure's projection lands in: the same one as its KPI's
primary measure, never a visual of its own.

- A Qlik object showing several INDEPENDENT values that are NOT a
  primary+secondary pair of the same KPI (a genuine "Multi-KPI" tile
  displaying several unrelated metrics at once) → N Card visuals, one per
  value shown, laid out within the original object's bounds — related to,
  but distinct from, the config-table-driven KPI Container pattern in Task
  D. Don't confuse this with the primary+secondary case above: the test is
  whether Qlik itself groups them as one KPI object with a comparison
  value, not just "more than one number visually near each other."

## Table totals and sort order — mirror Qlik exactly, never Power BI's own default
A `tableEx`/`pivotTable`'s Qlik source object has its own `layout.properties.totals`
(`{"show": true|false, "position": "...", "label": "..."}`) — Power BI's
`tableEx` visual defaults to SHOWING a totals row when nothing says
otherwise, so an explicit Qlik `totals.show: false` must be reproduced
explicitly, not left to that default (a real review found an unwanted
totals row/column in Power BI that Qlik never calculated at all). Set
`visual.objects.total` accordingly:
```json
"objects": {
  "total": [{"properties": {"totals": {"expr": {"Literal": {"Value": "false"}}}}}]
}
```
If genuinely unsure of the exact property name for a specific visual
type's totals toggle, still record Qlik's `totals.show` value in
`"notes"` rather than silently defaulting to Power BI's own behavior
either way.

**Sort order**: every dimension in a Qlik object's `qHyperCubeDef` carries
its own `qSortCriterias`/the resolved `qDimensionInfo[].qSortIndicator`
("A" = ascending, "D" = descending, by the dimension's own value; a
`qSortByExpression`/measure-based sort is also common — sort by whichever
measure/expression Qlik itself sorts by, not alphabetically by default).
Reproduce this as the visual's own sort: for a chart, order the
`queryState`'s dimension/measure the query is naturally sorted by matches
this; for `tableEx`/`pivotTable`, set the initial sort column/direction to
match. A review found charts and tables NOT sorted the way Qlik had them
(e.g. a weekly trend chart not sorted by week, a risk table not sorted by
risk value) — this is as important to reproduce as the data itself, since
an unsorted trend/ranking chart reads as meaningless or wrong even when
every number in it is correct.

## Filters (listbox / filterpane objects)
Each become a `slicer` visual whose `visual.query.queryState.Values`
projects the bound field; do not create a page-level filter unless the Qlik
object was explicitly a global/sheet-level filter (`layout.properties.scope`).

**Dropdown vs. checkbox-list style**: a Qlik listbox's
`layout.properties.layoutOptions.collapseMode` says which — `"always"`
means Qlik renders it COLLAPSED, click-to-expand (a dropdown), `"never"`
means always-expanded (a checkbox list). Power BI's own slicer default is
the checkbox-list style regardless of this setting, so a Qlik dropdown
filter silently becomes a checkbox list unless this is set explicitly. Set
it via the slicer's own style property:
```json
"objects": {
  "general": [{"properties": {"style": {"expr": {"Literal": {"Value": "'Dropdown'"}}}}}]
}
```
This exact PBIR property path is not independently verified against a
Desktop-authored reference file the way title/subTitle/totals above were —
if you're not fully confident of it, still set it AND record Qlik's own
`collapseMode` value in `"notes"` (e.g. `"Qlik listbox uses collapseMode:
'always' (dropdown) — verify the slicer's Style is set to Dropdown in
Power BI, the exact objects.general.style property path is unconfirmed"`)
so a person can fix the visual formatting by hand in Power BI Desktop's
own Format pane if the JSON property name turns out to be wrong, rather
than silently leaving every dropdown-style Qlik filter as a checkbox list
with no signal that anything was supposed to be different.

---

# Task D: KPI Container Config Table → Individual KPI Cards

Some Qlik apps don't build each KPI tile as its own native chart object.
Instead they use a **config table** — one row per KPI, with columns like
`Title`, `Measure`, `Bg Color` — read at runtime by a generic "KPI
container" extension that renders N tiles from N rows. The normal
sheet-object extraction never sees these as separate KPIs, since they're all
driven by one config table, not by individual chart objects. This has no
Power BI equivalent MECHANISM at all (PBIR visuals are static, not
data-driven templates) — you convert each row of that table into one real
Power BI KPI card definition, generated up front.

## Input you receive
One detected config table: `table` (name), `fields` (column names), `rows`
(every row as extracted, each a `{column_name: value}` dict) — plus the
app's real `measures` list (`measures.json`) and `variables` list
(`variables.json`) so you can resolve references.

## Output
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
circular-reference case below), or a color condition you couldn't resolve.

## Conversion rules

### Resolving the KPI's value expression
A config table's "measure" column holds a Qlik expression as a **string
value in the cell**, not a schema column — always resolve per-row:
- **Bare bracket reference**, the cell is exactly `[Measure Title]` → this
  names a real master measure. Look it up (case-insensitive) in the
  `measures` list; set `measure_name` to that measure's exact title. If no
  such master measure exists, treat it as the "ad-hoc" case below instead.
- **Any other expression** (contains `num(...)`, string concatenation `&`,
  `if(...)`, multiple bracket references, etc.) → this is itself a Qlik
  expression that needs full conversion, the same way Task A converts a
  master measure — apply those same rules (set analysis → `CALCULATE`,
  `if` → `IF`/`SWITCH`, `num(x, fmt)` → drop the wrapper and put `fmt` in
  the KPI's own format string) to produce `synthesized_measure`. Bracket
  references inside the expression (`[Achievement %]`) become DAX measure
  references (`[Achievement %]`) unchanged — Power BI resolves a bracketed
  name in a measure expression to another measure the same way Qlik does.
- A secondary "second measure"/comparison-text column (e.g. showing "▲ vs
  100% target") is a separate small KPI card feature (subtitle text) — treat
  it exactly the same way (resolve or synthesize) and include it as a
  second `synthesized_measure` in a `"subtitle_measure"` field if present in
  the row; omit that field if the table has no such column.

### Resolving colors
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

### Grid columns
If the table has an explicit "Sheet"/page column and a "KPI"/position
column, copy their raw values into `sheet`/`position` — the builder uses
these to lay the cards out in the right grid order. If there's no such
column, use `row_index` for `position` and leave `sheet` null.

### A KPI row referencing another KPI row by title
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

### ShowCondition
If a `ShowCondition`-style column is present and its value is a Qlik boolean
literal that's always false (`"0"`) for a row, omit that row entirely — it's
a KPI the Qlik author disabled. A value of `"1"` or a variable reference
means always show; include the row.

---

# Appendix: Qlik Expression → DAX Function Reference

Not exhaustive, but covers the highest-frequency general-purpose functions
beyond aggregation/set-analysis (already covered above). **Not all of these
are "Exact"** — Qlik expressions run in an associative evaluation context,
DAX runs in a filter-context/row-context model, so even a same-name-looking
function can behave differently at the edges (blanks, type coercion,
multi-value fields).

| Qlik | DAX / Power BI target | Notes |
|---|---|---|
| `If()` | `IF()` | Direct — same 3-argument shape. |
| `Pick()` | `SWITCH()` (index form) / a `SWITCH(TRUE(), ...)` chain | Qlik's `Pick(n, v1, v2, ...)` selects positionally; DAX has no positional-index `SWITCH`, must use `SWITCH(n, 1, v1, 2, v2, ...)`. |
| `Match()` | `SWITCH()` / equality-chain comparison | Qlik's `Match()` returns the 1-based position of the first match (or 0); DAX must reconstruct that position explicitly if the ordinal result itself (not just a resulting value) is used downstream. |
| `WildMatch()` | `CONTAINSSTRING()` / pattern-matching logic | Qlik wildcard matching (`*`/`?`) has no direct DAX equivalent function — needs decomposition into one or more `CONTAINSSTRING`/`SEARCH` calls depending on the pattern's complexity. |
| `Alt()` | `COALESCE()` / explicit fallback `IF`/`ISBLANK` chain | Qlik's `Alt()` returns the first argument that is a valid number; `COALESCE()` returns the first non-blank — close but not identical (numeric-validity vs. blank-ness). If the source field can hold non-numeric junk (not just missing values), `COALESCE(field, 0)` is WRONG — use `IF(ISERROR(VALUE(field)), 0, VALUE(field))` instead, since `COALESCE` only guards blank/null, not "not a number." |
| `IsNull()` | `ISBLANK()` | Safe as a direct mapping for the *test itself*, but two specific DAX-vs-Qlik divergences are real, verified, silent-bug traps once the result feeds arithmetic or a comparison — **never rely on default DAX blank-coercion to match Qlik's NULL propagation**: (1) **Arithmetic**: DAX `BLANK() + 5` evaluates to `5` (blank is the additive identity for `+`/`-`), whereas Qlik `NULL + 5` evaluates to `NULL`. Any Qlik expression that relies on NULL-propagating-through-`+`/`-` to suppress a result needs an explicit `IF(ISBLANK(x), BLANK(), x + 5)` guard in the DAX — `*`/`/` don't have this problem (`BLANK() * 5 = BLANK()`, matching Qlik). (2) **Equality**: DAX `BLANK() = BLANK()` evaluates to `TRUE`, whereas Qlik `NULL = NULL` evaluates to `NULL` (neither true nor false). Any Qlik `If(IsNull(x) and IsNull(y), ...)`-style guarded comparison needs an explicit `ISBLANK()` check on each side in the DAX rewrite, not a naive `x = y`, or two blank values will incorrectly compare equal. |
| `Len()` | `LEN()` | Direct. |
| `Left()` / `Right()` / `Mid()` | `LEFT()` / `RIGHT()` / `MID()` | Direct. |
| `Upper()` / `Lower()` / `Trim()` | `UPPER()` / `LOWER()` / `TRIM()` | Direct. |
| `Date()` | Date/Time **formatting** operation, not a type conversion | Formats an already-numeric/date value for display; a model/visual format string, not a DAX-side transformation. |
| `Date#()` | Power Query type conversion / date PARSING | Parses a text value into a real date — a Power Query (script_conversion) concern, not a DAX function. |
| `Num()` | Model/visual FORMAT STRING, not `FORMAT()` | See Task A's Formatting rule — never convert `Num()` to DAX `FORMAT()`. |
| `Today()` / `Now()` | `TODAY()` / `NOW()` | Direct. |
| `Year()` / `Month()` / `Day()` | `YEAR()` / `MONTH()` / `DAY()` | Direct. |
| `Week()` | `WEEKNUM()` | Direct, but confirm week-numbering system (ISO vs. US) matches the source app's intent. |
| `WeekDay()` | `WEEKDAY()` | Direct, but confirm the start-of-week convention matches. |
| `MonthStart()` | `DATE(YEAR(...), MONTH(...), 1)` | No single DAX function — reconstructed via `DATE()`. |
| `MonthEnd()` | `EOMONTH()` | Direct. |

# Appendix: Calculated Column vs. Measure vs. Parameter — Decision Tree

Qlik does not have the same measure/calculated-column distinction Power BI
enforces — a single Qlik expression pattern can legitimately need to become
any of several different Power BI constructs depending on WHEN/HOW it's
evaluated. Apply before picking a target in Task A/B:
```text
Qlik expression
  +-- evaluated per row, at LOAD time (fixed once loaded)?
  |       -> Power Query calculated column (script_conversion skill's concern)
  |
  +-- evaluated dynamically according to current selections/filter context?
  |       -> DAX Measure (Task A)
  |
  +-- used purely for grouping/category (not itself an aggregation)?
  |       -> Column / DAX Calculated Column (Task B)
  |
  +-- user-controlled (a slider/input, or an environment-specific value)?
          -> Power BI Parameter / disconnected What-If table (script_conversion skill's concern)
```
