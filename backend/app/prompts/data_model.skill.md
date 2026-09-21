---
name: qlik-to-powerbi-data-model
description: Convert Qlik's associative data model into explicit Power BI relationships/typed columns, and Section Access into RLS/OLS roles
---

# Role
Qlik associates tables automatically by matching field *names*, and can
restrict data per-user via `SECTION ACCESS`. Power BI needs **explicit**
relationships/typed columns declared in TMDL (Task A) and, separately,
Row-Level/Object-Level Security roles (Task B). The caller tells you which
task a given request is via the payload's `task` field (`"relationships"`
or `"rls"`) — apply only that task's rules below.

Reference: `qlik-bi-components/v1_powerbi-qlik-similarity-components.md` §2
(Data Model) and §1's Section Access rows document the full similarity
rationale behind every rule here.

# Critical: preserve exact names
Every table/column name in your output must be **copied verbatim** from the
input — same spelling, same casing. Never invent a name, never
normalize/clean one up, and never substitute a name from this document's own
illustrative examples (`TableName`, `Region`, `UserId`, etc. are
placeholders explaining the *pattern*, not real names to fall back on). A
renamed or invented name breaks silently downstream.

---

# Task A: Associative Model → Relationships

Qlik's associative model primarily creates **implicit** associations
through common field names — but Qlik can also physically combine tables
via `JOIN`/`KEEP`/`CONCATENATE` in script (see the script_conversion skill),
and it does not require the author to declare relationship
cardinality/direction the way Power BI does. Your job is to infer, for
every relationship: join column(s), cardinality, filter direction,
active/inactive status, and whether a bridge table is needed.

## Input you receive
`data_model.json` (`tables`: each with field list + row/field counts;
`keys`: the associative key fields Qlik detected between tables, i.e. fields
sharing the same name across ≥2 tables).

## Output
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

## Conversion rules
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
  `"notes"` so the sheets_convert conversion knows cross-filtering between
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

---

# Task B: Section Access → RLS / OLS

Convert Qlik's `SECTION ACCESS` load-script block into Power BI security.
**Section Access row-reduction → RLS is Partial, not Exact** (different
mechanism: Qlik reduces at reload/script time, RLS filters at query time),
and **`OMIT` (column-level) → Object-Level Security (OLS) is also only
Partial** (different implementation/administration model). **A Power BI
Perspective is NOT a security mechanism at all** — it is only a curated,
non-enforced view of the model; never treat it as an `OMIT` substitute.

## Input you receive
`section_access.json` (`present`, `raw_block`, `fields`, `has_userid`,
`has_omit`, `has_reduction`) plus the final data model's table/column names.

## Output
```json
{
  "roles": [
    {
      "name": "<RoleName>",
      "table_permissions": [
        {"table": "<TableName>", "filter": "<DAX boolean expression>"}
      ],
      "confidence": "high",
      "notes": "<optional — anything that needs manual verification>"
    }
  ],
  "notes": ["..."]
}
```
Per-role `confidence` is `"high"`/`"medium"`/`"low"` — use `"low"` for any
`LOOKUPVALUE`-based filter you built without being fully sure the referenced
security table/columns will exist in the final model, or a composite
reduction whose AND/OR semantics you had to infer.

## Conversion rules

### USERID-based reduction (most common pattern)
Qlik:
```
SECTION ACCESS;
LOAD * INLINE [
USERID, ACCESS, OMIT
alice@corp.com, USER, Region
...
];
```
This means: the `USERID` field is compared against the signed-in user, and
whatever value is in the row's reduction field(s) (here `Region`) restricts
what data that user sees, typically by joining `USERID`/reduction fields into
the fact table via a security table.

Power BI equivalent: a **dynamic RLS** role using `USERPRINCIPALNAME()`:
```
[Filter table permission on the security/dimension table that holds the reduction field, e.g. "Region" table]
'Region'[Region] = LOOKUPVALUE('Security'[Region], 'Security'[UserId], LOWER(USERPRINCIPALNAME()))
```
If the reduction field lives directly on a dimension table imported from the
same section-access-style list (i.e. you can build a `Security` table from
the `raw_block`'s inline rows), emit that table's DAX-facing name and note
that the build step should materialize a `Security` table in the semantic
model (one row per user/reduction-value pair) so the `LOOKUPVALUE` above
resolves.

### Composite reduction (multiple fields, combined)
Qlik:
```
SECTION ACCESS;
LOAD * INLINE [
USERID, ACCESS, OMIT_REGION, OMIT_DEPT
alice@corp.com, USER, Region, Department
...
];
```
When a user's row names more than one reduction field, Qlik applies them
together as an **AND** (the user sees only rows matching every one of their
reduction values simultaneously) — there is no Qlik idiom for combining
multiple `SECTION ACCESS` reduction fields with OR. Convert each reduction
field to its own `table_permission` entry against the table/dimension that
holds it; since a role's `table_permissions` are all independently-applied
row filters (and Power BI intersects filters across different tables the
same way Qlik intersects reduction fields), listing one entry per field
already reproduces the AND semantics — don't try to combine them into one
compound DAX expression unless both reduction fields live on the *same*
table, in which case AND them directly in one `tablePermission` filter:
`'Fact'[Region] = LOOKUPVALUE(...) && 'Fact'[Department] = LOOKUPVALUE(...)`.

### Admin/override access (bypasses reduction entirely)
If the inline table's `ACCESS` column has a value like `ADMIN` (vs `USER`)
for some rows, and those rows have blank/empty reduction field values (no
`Region`/`Department` etc. — the common way Qlik apps signal "sees
everything"), that role should get **no row restriction at all**, not a
filter that happens to be blank. Emit that role's `table_permissions` with a
filter of `TRUE()` (or omit `table_permissions` for that table entirely) —
never leave it pointing at a `LOOKUPVALUE(...)` that would evaluate to
`BLANK()`/no-match for an admin user, which would silently show them *zero*
rows instead of *all* rows.

### Static OMIT (no per-user dynamic lookup, a fixed field is universally hidden)
If `OMIT` names a field that's simply removed for everyone (not tied to
USERID), that's a genuine column-level concern — closest Power BI mechanism
is Object-Level Security (OLS) on that column/table, which is not natively
exposed through Power BI Desktop's role UI (typically configured via
Tabular Editor or XMLA against the published model). Call this out
explicitly under `"notes"` as an OLS candidate rather than an RLS role, and
do not fabricate an RLS filter that only approximates hiding a column —
row filtering cannot hide a column.

### Dynamic/per-user OMIT (column visibility differs by user/role)
If `OMIT` varies per-user (different users have different omitted fields,
per the inline table), this is the strongest case for OLS over a structural
modeling workaround — note each distinct OMIT set as its own OLS
candidate/role grouping under `"notes"`, since Power BI Desktop alone can't
assign this without an OLS-capable tool.

### REDUCTION
Qlik `REDUCTION Field;` clauses reduce the loaded rows themselves, not
per-viewer visibility — if this is the only Section Access mechanism found (no
`USERID` companion), it means the row reduction already happened once at
extract time and there is nothing left to model as RLS; note this explicitly
rather than fabricating a role.

### No Section Access present
When `present` is `false`, return `{"roles": [], "notes": ["No Section Access in source script — no RLS to migrate."]}`.

### Naming
Default role name: `"RestrictedAccess"` unless multiple distinct ACCESS
levels are present in the inline table (e.g. `ADMIN` vs `USER`), in which case
emit one role per distinct ACCESS value, named after that value
(`"Admin"`, `"User"`).

### Manual follow-up (out of this skill's scope, still worth noting)
Generating the role/filter definition here is only "artifact" migration.
Actually assigning real users/groups to these roles, and reviewing any
`OMIT`/OLS requirement against the organization's actual security policy,
happens manually in the Power BI Service after deployment — always include
a `"notes"` reminder of this rather than implying the role definition alone
is a complete migration of Section Access.
