---
name: qlik-to-powerbi-data-model
description: Convert Qlik's associative field-name-matching data model into an explicit Power BI star-schema-style relationship graph with typed columns
---

# Role
Qlik associates tables automatically by matching field *names*. Power BI needs
**explicit** relationships and typed columns declared in TMDL. You produce the
`relationships.tmdl` content and the per-table column type/key annotations
needed to reproduce the same associations.

# Critical: preserve exact names
Every table name and column name in your output must be **copied verbatim**
from `data_model.json` — same spelling, same casing. Never invent a name,
never normalize/clean one up, and never substitute a name from this
document's own illustrative examples (`TableName`, `ColumnName`, `Date`,
etc. are placeholders explaining the *pattern*, not real names to fall back
on). A renamed or invented table/column name breaks silently downstream —
the relationship looks plausible right up until the builder can't find that
table.

# Input you receive
`data_model.json` (`tables`: each with field list + row/field counts;
`keys`: the associative key fields Qlik detected between tables, i.e. fields
sharing the same name across ≥2 tables).

# Output
A JSON object:
```json
{
  "relationships": [
    {"name": "...", "from_table": "...", "from_column": "...", "to_table": "...", "to_column": "...", "cardinality": "one-to-many", "cross_filter": "single", "confidence": "high", "notes": "<optional>"}
  ],
  "column_types": {
    "TableName": {"ColumnName": "int64|string|double|dateTime|boolean"}
  },
  "key_columns": {"TableName": ["ColumnName", ...]}
}
```
`confidence` on a relationship is `"high"`/`"medium"`/`"low"` — use `"low"`
when you had to pick a "many"/"one" direction without clear distinct-value
evidence, resolve a circular-reference loop by guessing the weakest link, or
guess at a link-table/date-island classification. `column_types` and
`key_columns` are data-driven from `qTags`, not judgment calls, so they
don't carry a confidence field.

# Conversion rules
- Every entry in `keys` becomes one relationship. Pick the table with more
  distinct key values as the "many" side (`from_table`) and the smaller
  lookup-style table as the "one" side (`to_table`) — mirrors how Qlik's
  associative engine behaves and matches typical Power BI star-schema
  direction (fact → dimension).
- If a key field appears in more than 2 tables (a shared conformed dimension,
  e.g. `Date`), create one relationship per pair, but mark all but the first
  as `"is_active": false` if Power BI would otherwise reject ambiguous active
  paths between the same two tables — note this explicitly in a `"notes"`
  array in your JSON output so the builder can wire `USERELATIONSHIP` where
  needed in DAX measures.
- Synthetic keys (Qlik auto-concatenated composite keys named `%KeyN` or
  `$Syn`) should be resolved back to their original composite fields — emit
  one relationship per real key field pair instead of the synthetic key.
- Infer `data_type` per field from sampled values / field tags in
  `data_model.json` (`qTags` containing `$numeric`, `$date`, `$timestamp`,
  `$text`) → map `$numeric` → `double` (or `int64` if all sampled values are
  integral), `$date`/`$timestamp` → `dateTime`, otherwise `string`.
  **Exception**: a field tagged `$date`/`$timestamp` whose name contains
  "Name" (e.g. `InvoiceMonthName`, from Qlik's `MonthName()`/`WeekDayName()`
  idiom) is a dual value used for its display TEXT ("Aug 2026"), not the
  underlying date serial — classify it `string`, not `dateTime`. A sibling
  field without "Name" in it (e.g. `InvoiceMonthNum`) holding the same
  information as a real number stays numeric as normal.
- Fields used only as join keys should be flagged in `key_columns` so the
  builder can set `isKey: true` / hide the raw key column when a friendlier
  display column exists.
- Cardinality: always `"one-to-many"` — never `"one-to-one"`, even when both
  sides happen to have equally high distinct-value ratios. `from_table` is
  the many/fact side, `to_table` is the one/dimension side (the builder
  renders this as plain, un-asserted many-to-one TMDL either way — proper
  star-schema shape). A genuinely 1:1-looking pair of tables is a sign they
  could be merged, not a reason to declare the relationship 1:1: asserting
  1:1 has repeatedly caused Power BI Desktop to reject the whole project on
  open ("Relationship '<guid>' uses an invalid column ID <n>") with no
  corresponding benefit — every real DAX use case (RELATED() reading a
  dimension-side column while iterating the fact side) already works under
  plain many-to-one, no 1:1 required. If two tables share a key and BOTH
  look like fact tables (e.g. one invoice table, one payments-per-invoice
  table), still pick whichever is the natural "many" side (the one that can
  have multiple rows per key, even if today's sample happens not to) as
  `from_table`.
- Cross-filter direction: default `"single"` (dimension filters fact); only
  use `"both"` when the Qlik app's associative behavior clearly requires
  bidirectional filtering between two dimension-like tables (rare — flag with
  a note rather than guessing silently).
- **Circular reference loops** (3+ tables sharing keys that form a cycle,
  e.g. `A.CustomerID = B.CustomerID`, `B.RegionID = C.RegionID`,
  `C.CustomerID = A.CustomerID` closing the loop): Qlik's associative engine
  tolerates this (it isn't a strict relationship graph), but a Tabular model
  cannot — a cyclical relationship path is rejected outright. Break the
  cycle by picking the *weakest* link (the pair with the lowest distinct-key
  overlap relative to table size — usually the least core to the star
  schema) and either omit that one relationship entirely or mark it
  `"is_active": false`, and say which pair and why in `"notes"` so it's
  clear this wasn't dropped by accident.
- **Link tables** (a bridge table that exists only to join two fact tables
  on a shared grain, e.g. `Fact_Orders`/`Fact_Shipments` joined through an
  `OrderShipmentLink` table rather than sharing a key directly): keep the
  link table as its own table with `"one-to-many"` relationships to *both*
  fact tables (link table as the "one" side on each), don't try to collapse
  it into a single direct fact-to-fact relationship — Power BI's
  single-direction-per-hop model needs the bridge exactly the way Qlik's
  synthetic association would have used it. Flag it as a link table in
  `"notes"` so the report_visuals conversion knows cross-filtering between
  the two facts goes through it.
- **Date islands** (a date/calendar table that is deliberately *not*
  connected to the rest of the model — e.g. a standalone "reference
  calendar" used only for what-if date-picker slicers, not for filtering
  facts): don't force a relationship onto it just because it shares a
  `Date`-named field with a fact table if the Qlik script/app clearly keeps
  it disconnected (no shared key was actually detected in `keys`, or a
  script comment says so). Fabricating a relationship the source app didn't
  have risks silently changing what a visual filters. Leave it unconnected
  and note it as an intentional date island.
