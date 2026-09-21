---
name: qlik-script-to-powerbi
description: Convert a Qlik load script (script.qvs) into Power Query M partitions and Power BI parameters/measures — covers both per-table M queries and script/UI variables
---

# Role
You convert everything that lives in a Qlik **load script** (`script.qvs`)
itself into its Power BI equivalent: per-table Power Query **M** partitions
(Task A) and script/UI **variables** into Power Query Parameters, DAX
measures, or What-If parameters (Task B). The caller tells you which task a
given request is via the payload's `task` field (`"m_query"` or
`"variables"`) — apply only that task's rules below.

Reference: `qlik-bi-components/v1_powerbi-qlik-similarity-components.md` §1
(Load Script ↔ Power Query M) and §3 (Variables) in this repo document the
full Qlik↔Power BI similarity rationale behind every rule below — consult it
for the "why," not just the "what."

# Critical: preserve exact names
Every field/column/table/variable name your output references must be
**copied verbatim** from the input — same spelling, same casing. Never
invent a name, never normalize/clean one up, and never substitute a name
from this document's own illustrative examples (`OrderID`, `CustomerID`,
`Amount`, `vMaxYear`, `vCurrency`, etc. are placeholders explaining the
*pattern*, not real names to fall back on). A renamed or invented name
breaks silently — it looks plausible right up until Power BI's Refresh
fails to find it, or a measure downstream can't resolve it.

---

# Task A: Load Script → M Query (per table)

## Input you receive
- The full Qlik load script (`script.qvs`) for context (variables, connections).
- The name of the target table.
- The specific statements in the script that populate that table (LOAD /
  RESIDENT / JOIN / CONCATENATE / CROSSTABLE chain).

## Output
A single M expression, starting with `let` and ending with `in <name>`. No
markdown fences, no commentary — just the M code, ready to paste into
`source = <this>` in a TMDL partition block.

**Exception — flagging low confidence**: if you had to guess at an
unavailable source (the QVD's true origin isn't in the script), invent a
best-effort join/transform for something ambiguous, or otherwise aren't
confident this M reproduces the original table exactly, prepend exactly one
line before `let`:
```
// CONFIDENCE: low - <one-sentence reason>
```
(or `medium` for a smaller doubt). Omit this line entirely when confident —
don't add it by default. This is parsed out and stripped before the M is
used, so it never ends up in the actual partition source; it only feeds the
same review-flagging every other task's `confidence` field does.

## Conversion rules

### Data sources
- `LOAD ... FROM [lib://Connection]path\file.qvd (qvd);` → `QvdFile.Contents` doesn't
  exist in real M; instead treat QVD sources as the *upstream* extract and load
  from the same origin the QVD was built from when it's declared in the script
  (e.g. the preceding `LOAD ... FROM` on a CSV/Excel/DB). If the true origin is
  unknown, load the QVD's sibling flat file if present, otherwise emit
  `Csv.Document(File.Contents("<path>"))` / `Excel.Workbook(File.Contents("<path>"))`
  as the best-effort equivalent and leave a one-line `// TODO source:` comment
  above `let` (comments above `let` are fine — they're stripped by the caller
  before this becomes a partition source, but keep the expression itself
  comment-free).
- `LOAD ... FROM [lib://Connection](qvd)` where the qvd is clearly a cache of a
  DB table → prefer `Odbc.DataSource` / `Sql.Database` reading the same table
  when the connection string / SQL is present in the script; otherwise fall
  back to the flat-file approach above.
- `SELECT ... FROM ...;` (native DB LOAD) → `Value.NativeQuery(Sql.Database(server, database), "<same SQL>")`.
- Inline `LOAD * INLINE [...]` → `#table(type table [...], {{...}, ...})`.
- A source connected as Qlik DirectQuery / a live connection (ODBC, OLE DB,
  REST, SQL, web file) rather than loaded into Qlik's in-memory engine: flag
  this in a `// NOTE:` comment above `let` — the closer Power BI target may
  be DirectQuery mode against the same source rather than an imported M
  query; this decision is out of this skill's scope (it's a model-mode
  choice, not per-table M text) but must not be silently assumed away.

### RESIDENT / chains
- `LOAD ... RESIDENT TableA WHERE <cond>;` → `Table.SelectRows(TableA, each <cond as M predicate>)`.
- `LOAD field1, field2 RESIDENT TableA;` (field subset) → `Table.SelectColumns(TableA, {"field1","field2"})`.
- Multiple RESIDENT steps chaining off one table → nest as sequential `let` steps.

### CROSSTABLE
`CROSSTABLE (AttributeField, DataField, N) LOAD key1, key2, col1, col2, ... FROM ...;`
→ `Table.UnpivotOtherColumns(Source, {"key1","key2", ...first N id columns}, "AttributeField", "DataField")`.

### JOIN / CONCATENATE / KEEP / NOCONCATENATE
- `JOIN (TableA) LOAD ... RESIDENT TableB;` → `Table.Join(TableA, {"<keys>"}, TableB, {"<keys>"}, JoinKind.Inner)` — pick `JoinKind.LeftOuter` if the Qlik statement is `LEFT JOIN`. `JOIN` physically merges TableB's columns into TableA.
- `CONCATENATE (TableA) LOAD ... ;` → `Table.Combine({TableA, <new rows source>})`.
- `NOCONCATENATE LOAD ...;` — this statement SUPPRESSES Qlik's automatic
  concatenation (an unlabeled `LOAD` that produces a table with identical
  field structure to the previous one would otherwise auto-merge onto it).
  Emit the `NOCONCATENATE`-prefixed LOAD as its own **separate** M query
  (its own `let...in` producing its own named table) — never combine it
  into the preceding table's `Table.Combine`/steps, even though the field
  structure looks like it could auto-merge. The author deliberately kept
  these two tables apart.
- `LEFT KEEP (TableA) LOAD ... RESIDENT TableB;` / `INNER KEEP` / `OUTER KEEP`
  — **`KEEP` is NOT the same operation as `JOIN`.** `JOIN` merges columns
  from both tables into one. `KEEP` only RESTRICTS WHICH ROWS remain in
  EACH table (based on matching keys) — the two tables stay separate, with
  their own distinct columns, just filtered down to matching key values.
  Convert as a semi-join filter, not a merge:
  ```
  -- LEFT KEEP (TableA) ... RESIDENT TableB  (TableB rows filtered to keys present in TableA)
  KeptTableB = Table.SelectRows(TableB, each List.Contains(TableA[<key>], [<key>]))
  ```
  For `INNER KEEP`, apply the equivalent row-restriction to BOTH tables
  (each filtered to keys present in the other); for `OUTER KEEP`, no
  filtering happens — treat as a no-op association hint. Keep both
  resulting tables as separate M queries with their own original columns —
  do not merge their columns the way `JOIN` would.

### CrossTable / Inline Load
- Covered above (CROSSTABLE, INLINE).

### Mapping tables + ApplyMap
A Qlik `Mapping LOAD` table is dropped from the data model after use — it
never becomes a real table Power BI would show, `ApplyMap('MapName', Field,
default)` is purely a lookup applied at load time. Convert by inlining the
mapping's actual key→value pairs (read from wherever the mapping table's
source is: another table's data, or an inline list) directly into a
`Table.AddColumn` step using `List.PositionOf`/a static record, not as a
separate model table:
```
MapTable = #table({"Key","Value"}, {{"US","United States"}, {"UK","United Kingdom"}, ...}),
MapDict  = Record.FromList(MapTable[Value], MapTable[Key]),
Applied  = Table.AddColumn(Source, "CountryName", each
    try Record.Field(MapDict, [CountryCode]) otherwise "Unknown"   // "Unknown" = ApplyMap's default arg
)
```
If the mapping table's source data isn't available in this conversion call,
note the gap under a `// TODO source:` comment above `let` rather than
fabricating placeholder key/value pairs.

### SubField
`SubField(text, sep, N)` (Qlik, **1-based**) → `Text.Split(text, sep){N-1}`
(M, **0-based**) — always convert the index; this off-by-one is the single
most common bug risk in this conversion.

### Field-level transforms
- `Date(field, 'format')` / `Num(field, 'format')` → wrap with `Table.TransformColumns(Source, {{"field", each Date.From(_), type date}})` (or `Number.From`). **Never** convert a `Num()`-style display-formatting call into a DAX `FORMAT()` function downstream (that belongs to the sheets task, not this one) — `Date()`/`Num()` here are read/type operations on the M side, not the final display formatting, which stays a numeric/date value with a separate format string applied at the model/visual level.
- `Dual(text, num)` → keep the numeric column as the value; store `text` in a
  parallel display column if both are referenced downstream, otherwise drop
  the dual wrapper (Power BI has no native dual type — use format strings on
  the DAX side instead).
- `AutoNumber(expr [, 'AutoID'])` → this is a DIFFERENT mechanism from
  `Dual()` and must not be treated as the same thing. `AutoNumber()` alone
  (not wrapped in `Dual()`) produces a stable integer surrogate key from an
  arbitrary expression — reproduce it as `Table.AddIndexColumn` if it's
  purely positional, or, when it's hashing a text/composite value into a
  stable key used for relationships elsewhere, prefer keeping the original
  text value as the join key instead of trying to reproduce Qlik's specific
  integer-assignment algorithm (Power BI relationships only need both sides
  to agree on one value, and a text key is easier to reproduce faithfully
  than the numeric ID Qlik happened to assign row-by-row during its own load).
- Renames via `AS` → `Table.RenameColumns(Source, {{"OldName","NewName"}})`.
- `RENAME FIELD OldName TO NewName;` (a standalone script statement, not an
  inline `AS`) → also `Table.RenameColumns(Source, {{"OldName","NewName"}})`,
  applied as its own step. **Flag it**: a standalone `RENAME FIELD` is
  sometimes used specifically to BREAK an unwanted automatic association
  between two same-named fields — if `OldName` also appears as a field name
  in another table being converted in this same app, add a one-line
  `// NOTE:` comment above `let` mentioning the rename, so the data-model
  step doesn't accidentally re-associate what this rename was deliberately
  separating.
- `LOAD DISTINCT ...` → `Table.Distinct(Source)`.
- `DROP FIELD field1[, field2, ...];` / `DROP FIELDS ...;` →
  `Table.RemoveColumns(Source, {"field1", "field2", ...})`. If the dropped
  field is a name that also appears in another table (a would-be relationship
  key), add a `// NOTE:` comment above `let` — the drop may have been
  intentionally preventing an association.
- `DROP TABLE TableName;` → do not emit an M query for that table at all. A
  Qlik script commonly builds a temporary/RESIDENT table, uses it in a
  later JOIN/CONCATENATE/KEEP, then drops it — if `TableName` is only ever
  used as an intermediate step feeding another table's conversion (never
  the actual target table you were asked to convert), fold its logic into
  that target table's own `let` chain as intermediate steps instead of
  producing a separate standalone query for it.

### Variables referenced inside this table's LOAD (vMaxYear, vCurrency, ...)
Do not inline a variable's value. If the LOAD statement references a script
variable, leave the M expression parameterized by referencing a Power Query
**Parameter** of the same name (keep the Qlik name, e.g. `vMaxYear`) — that
parameter is created separately by Task B; your M can simply reference
`vMaxYear` as a bare identifier in scope.

### IntervalMatch (bucketing a value into a range table)
`IntervalMatch(Amount) LOAD LowerBound, UpperBound RESIDENT Bands;` assigns
each fact row's value to the range it falls inside, producing a new
association — there's no direct M equivalent function, so build it as an
explicit range join, then select the containing row:
```
IntervaledJoined = Table.AddColumn(FactTable, "BandRow", each
    Table.SelectRows(Bands, (b) => b[LowerBound] <= [Amount] and [Amount] <= b[UpperBound])
),
BandExpanded = Table.ExpandTableColumn(IntervaledJoined, "BandRow", {"BandName"}, {"BandName"})
```
(substitute the real column names). This is O(n×m) — call it out in a
`// perf:` note above `let` if the fact table is large, since a
merge-based range join may be needed instead for real performance, but keep
the expression itself producing correct results first.

### QUALIFY / UNQUALIFY
These Qlik statements control whether the *upcoming* `LOAD`s prefix field
names with their table name (to avoid Qlik's automatic same-name
association) — they're a script-authoring directive, not a data
transformation. They don't affect the resulting table's actual columns or
values, so they need no M equivalent at all — just use the field names as
they appear in the final loaded table (post-qualification, if the script
qualified them, the field names in `data_model_table` already reflect that).
`QUALIFY` is a deliberate relationship-suppression signal, though — flag its
presence in a `// NOTE:` comment above `let` so the data-model task doesn't
try to re-associate fields the script author intentionally qualified apart.

### Row-context filters that belong to Section Access
Skip WHERE clauses that only exist to enforce Section Access (matched against
`section_access.json`); those become RLS DAX filters instead (Task B of the
data-model skill), not M filters.

## Example

Qlik:
```
Sales:
LOAD
    OrderID,
    CustomerID,
    Date(OrderDate) as OrderDate,
    Amount
FROM [lib://DataFiles]Sales.qvd (qvd);

LOAD OrderID, CustomerID, OrderDate, Amount
RESIDENT Sales
WHERE Amount > 0;
```

M:
```
let
    Source = Csv.Document(File.Contents("Sales.csv"), [Delimiter=",", Columns=4, Encoding=1252, QuoteStyle=QuoteStyle.None]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),
    Typed = Table.TransformColumnTypes(Promoted, {{"OrderID", Int64.Type}, {"CustomerID", Int64.Type}, {"OrderDate", type date}, {"Amount", type number}}),
    Filtered = Table.SelectRows(Typed, each [Amount] > 0)
in
    Filtered
```

---

# Task B: Script/UI Variables → Parameters / Measures

Qlik variables serve several different purposes that need different Power
BI equivalents. Decide which one applies per variable and produce the
matching artifact.

## Input you receive
The full `variables` array from `variables.json` (`name`, `definition`,
`comment`, `is_script_created` per item) plus `script` (the full load script,
so you can see where else each variable name appears) — used to decide each
variable's role.

## Output
Convert every item in the input array. Return:
```json
{"variables": [
  {
    "name": "vMaxYear",
    "target": "power_query_parameter" | "dax_measure",
    "table": "<owning table, dax_measure target only>",
    "power_query": {"type": "Number.Type|Text.Type|Date.Type", "current_value": "...", "derived_m_expression": null},
    "dax": {"expression": "..."},
    "confidence": "high",
    "notes": "<optional — anything that needs manual verification>"
  }
]}
```
`confidence` is `"high"`/`"medium"`/`"low"` — use `"low"` when you can't
tell whether a variable is a UI-slider target vs. a plain constant, or when
resolving a set-analysis-fragment variable's inline substitution required
guessing which measures reference it.
Include only the `power_query` or `dax` block matching that item's `target`.
`notes` is optional on every item — include it whenever something couldn't
be resolved deterministically and needs a person to confirm it.

## Decision rule
- **Power Query parameter or query-derived scalar** when the variable holds
  a *static or load-time* value used only inside the load script
  (referenced inside `LOAD`/`WHERE` clauses, connection strings, or file
  paths). Two sub-cases, not one:
  - A **fixed constant** the author set once (e.g. `vCurrency: 'USD'` used
    in `WHERE Currency = '$(vCurrency)'`) → a Power Query **Parameter**
    (`target: "power_query_parameter"`).
  - A value **computed from the data itself during load**, re-evaluated
    fresh every reload (e.g. `LET vMaxDate = Max(OrderDate)` read from a
    resident table, then used later in the script) → this is NOT
    automatically a DAX measure just because it's dynamically computed — it
    still belongs to the load/transform layer. Emit it as a
    **query-derived scalar** instead: `target: "power_query_parameter"`
    with `"power_query": {"type": "...", "current_value": null,
    "derived_m_expression": "<M expression the caller can drop into its
    own let-step, e.g. List.Max(Source[OrderDate])>"}`, and note under
    `"notes"` that this must be wired as its own query/step (evaluated
    during refresh) rather than a fixed user-set Parameter value.
- **DAX measure** when the variable's definition is itself an *aggregation or
  set-analysis expression* evaluated dynamically against the current
  selection — e.g. `vMaxYear: =Max(Year)` used inside other measures'
  set-analysis (`{<Year={$(vMaxYear)}>}`). These must stay dynamic in Power BI
  too, so they become a DAX measure (often marked hidden, used only inside
  other measures via `[vMaxYear]`).
- If a variable is used in **both** contexts (load script and dynamic
  expressions), split it: emit both a Power Query parameter (for the load
  script's static default) and a DAX measure (for dynamic use in other
  measures), and say so under `"notes"`.

## Conversion rules

### Power Query parameter
- Infer M type from the variable's literal value: numeric literal → `Number.Type`,
  quoted string → `Text.Type`, `Date/Now()/Today()` calls → `Date.Type`.
- `current_value` is the literal, unwrapped of Qlik quoting, e.g. Qlik
  `vCurrency: 'USD'` → `current_value: "USD"`.
- Any M query (from Task A) that referenced this variable name as
  a bare identifier will resolve against this parameter automatically once
  it's declared in the Power Query `Parameters` group — no further wiring
  needed from this task.

### DAX measure
- `vMaxYear: =Max(Year)` → `expression: "CALCULATE(MAX(Sales[Year]), ALL(Sales[Year]))"`
  (use `ALL` on the relevant table so the "max" ignores the current filter
  context, matching Qlik's variable-computed-once-then-reused semantics,
  unless the variable is meant to react to selections — check the comment/
  usage context to decide whether to wrap with `ALL`).
- Mark these hidden (`is_hidden: true` in the resulting TMDL measure) unless
  the variable is also directly displayed as a KPI somewhere in the app.

### Interactive UI-triggered variable (slider / input box), not just script-time
A variable can be driven by a sheet control the user manually operates at
runtime — a slider (`qlik-variable-input` or similar extension, with
`min`/`max`/`step` properties bound to a `variableName`) or an input box —
rather than ever being computed from an expression or set once by the
script. This is a **third target**, distinct from both cases above: it
needs an interactive numeric range, which in Power BI is a small parameter
table + `SELECTEDVALUE()` measure (a what-if parameter), not a plain DAX
measure or load-time constant.
- Detect it by a sheet object bound to this variable name via a
  `min`/`max`/`step`-style property (not by the variable's own definition,
  which is just its current numeric value, e.g. `vSalesAchievement%: 100`
  looks like an ordinary constant until you see the slider referencing it).
  This case is normally handled directly from the sheet's own layout data
  before it reaches this skill — call it out here for completeness: if a
  variable's `comment`/usage context clearly describes it as a manually
  adjustable range (rather than a fixed default), flag it in `"notes"`
  (`"target": "dax_measure"` with a note "may be a UI slider — verify
  against sheet objects") rather than silently treating it as a static
  aggregation-based measure.

### Variable holding a full set-analysis expression (`$(=vFilter)` usage)
Some variables store an entire set-analysis clause as their definition, not
a scalar value — e.g. `vFilter: ={<Status={'Active'}, Region={'$(vRegion)'}>}`
— then get spliced into other measures as `Sum({$(vFilter)} Amount)`. This
is different from the `vMaxYear`-style "aggregation variable" case: the
variable's *text* is itself a set-analysis condition, so it can't become one
self-contained DAX measure — inline the resolved filter conditions directly
into whatever measure references it, the same way the sheets_convert skill
converts an inline set-analysis clause:
- `vFilter: ={<Status={'Active'}, Region={'$(vRegion)'}>}` used in
  `Sum({$(vFilter)} Amount)` → when converting THAT measure, resolve
  `vFilter`'s definition first and inline its conditions:
  `CALCULATE(SUM(Sales[Amount]), Sales[Status] = "Active", Sales[Region] = <vRegion's current value>)`.
- For this task's own output, emit `"target": "dax_measure"` with
  `"dax": {"expression": null}` and a `"notes"` entry explaining this
  variable is a set-analysis fragment meant to be inlined into other
  measures, not evaluated standalone — so the builder doesn't try to create
  a real measure from an expression that isn't valid DAX on its own.
