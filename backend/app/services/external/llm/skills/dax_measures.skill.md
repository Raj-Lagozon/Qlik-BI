---
name: qlik-measures-to-dax
description: Convert Qlik master measures (Sum, Count, Set Analysis) into DAX measures
---

# Role
Convert each Qlik master measure expression into an equivalent DAX measure
definition for a TMDL `measure` block.

# Critical: preserve exact names
Every table name, column name, and measure title you write into your output
must be **copied verbatim** from the input JSON — same spelling, same
casing, same punctuation. Never invent a name, never "clean up" or
normalize one, and never substitute a name from one of this document's own
illustrative examples (`Sales`, `Sales[Amount]`, `Sales[Region]`, etc. are
placeholders for explaining the *pattern* — they are not real names to fall
back on). If a measure's expression needs a table qualifier and the exact
table isn't given, resolve it from the accompanying `data_model` input's
real table/field names only; if it still can't be determined, say so in
`"notes"` and drop confidence rather than guessing a table name that sounds
plausible. A renamed or invented field/table name breaks silently — the
converted measure will look reasonable right up until Power BI can't find
the field it references.

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

# Cross-table row-by-row calculations (SUMX + RELATED)

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

# Input you receive
The full `measures` array from `measures.json` (`title`, `expression`,
`label_expression`, `tags` per item) plus `data_model` (tables/fields) so you
can qualify fields correctly (`Table[Column]`) and decide which table should
own each converted measure (the table that owns the fields the expression
aggregates).

# Output
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

# Conversion rules

## Bare variable substitution — `$(vVarName)`
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
converted elsewhere (by the variables/parameters conversion, possibly as an
interactive what-if slider) is a *specific, separate* artifact, not a stand-
in for the nearest similarly-named measure, and guessing wrong here silently
breaks the interactivity the app was built around (a manually-adjustable
slider becomes a fixed, non-reactive value with no visible error).
Instead, preserve the **exact original variable name** as a bracket
reference, unchanged: `$(vSalesAchievement%)` → `[vSalesAchievement%]`.
Resolving that name to whatever real DAX measure/parameter the variable
became is handled downstream by name-matching against the variable
conversion output — that's a deterministic lookup by the variable's own
name, which only works if you didn't substitute a different name in its
place.
- `($(vSalesAchievement%) / 100) * 12000` → `([vSalesAchievement%] / 100) * 12000`
- `Sum(Amount) * $(vTaxRate)` → `SUM(Sales[Amount]) * [vTaxRate]`

## Basic aggregations
- `Sum(Amount)` → `SUM(Sales[Amount])`
- `Count(OrderID)` → `COUNT(Sales[OrderID])`
- `Count(DISTINCT CustomerID)` → `DISTINCTCOUNT(Sales[CustomerID])`
- `Avg(x)` / `Min(x)` / `Max(x)` → `AVERAGE` / `MIN` / `MAX`
- `Sum(Amount)/Count(OrderID)` → same division of `SUM`/`COUNT` measures.

## Set analysis → CALCULATE / filter context
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
  than 1 DAX trick, and note it in `"description"`.

## Dynamic date-range idioms (MTD / QTD / YTD / rolling window)
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

## TOTAL qualifier
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

## RangeSum / accumulation
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

## Rank() — standalone ranking
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

## Aggr() — chart-level aggregation
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
- Nested `Aggr(Aggr(...))` (aggregate-of-an-aggregate, e.g. rank-of-ranks) →
  nest the same `SUMMARIZE`/`ADDCOLUMNS` pattern one level deeper; if the
  nesting is more than 2 levels deep, note it in `"description"` rather than
  producing an unverifiable wall of nested `ADDCOLUMNS` — this is a case
  worth a person double-checking against the Qlik original.

## Alternate states used directly in a formula
Qlik alternate states (`Set1`, defined via the sheet's "Alternate States"
panel, then referenced as `Sum({Set1} Amount)` inside an actual measure
expression — not just the "two independent slicer states" case already
covered above) don't have a DAX equivalent filter context, because a DAX
measure only ever sees one (the report's) filter context at evaluation
time. Convert by picking the concrete, named condition that state
represents in the app (check the sheet's alternate-state field selections in
context) and inlining it as an ordinary `CALCULATE` filter — e.g. if `Set1`
is documented/observed as "Region = West":
`Sum({Set1} Amount)` → `CALCULATE(SUM(Sales[Amount]), Sales[Region] = "West")`.
If the state's actual condition can't be determined from the extracted
data, don't guess — emit `CALCULATE(SUM(Sales[Amount]))` (current context,
the closest safe default) and flag it clearly in `"description"` as needing
manual review, since silently guessing the wrong region/segment here is
worse than leaving it as the ambient filter.

## P() / E() set functions
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

## IF / conditional logic
- `If(condition, true_expr, false_expr)` → `IF(<condition>, <true_expr>, <false_expr>)`, translating Qlik boolean operators (`=`, `<>`, `and`, `or`) 1:1 (DAX uses the same comparison operators; `and`/`or` map to `&&`/`||` or `AND()`/`OR()`).

## Formatting
Carry over Qlik's number format (from the measure's label/format tag if
present) into `format_string` using DAX/Power BI format-string syntax, e.g.
Qlik `#,##0.00` stays `#,##0.00`; Qlik `$#,##0;-$#,##0` → `"$"#,##0;-"$"#,##0`.

## Naming
Keep the same measure `title` as the DAX measure name so visuals map 1:1.
