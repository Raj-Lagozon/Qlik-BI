---
name: qlik-section-access-to-rls
description: Convert a Qlik SECTION ACCESS block (USERID, OMIT, REDUCTION) into Power BI Row-Level Security roles
---

# Role
Convert Qlik's `SECTION ACCESS` load-script block into one or more Power BI
RLS roles (TMDL `role` definitions with `tablePermission` DAX filters).

# Critical: preserve exact names
Every table name and column name in a `tablePermission` filter must be
**copied verbatim** from the final data model's real table/column names —
same spelling, same casing. Never invent a name, never normalize/clean one
up, and never substitute a name from this document's own illustrative
examples (`Region`, `Security`, `UserId`, etc. are placeholders explaining
the *pattern*, not real names to fall back on unless the app genuinely has
a table by that name). A renamed or invented table/column name breaks
silently — the role looks plausible right up until it's applied and
`LOOKUPVALUE` can't find the table.

# Input you receive
`section_access.json` (`present`, `raw_block`, `fields`, `has_userid`,
`has_omit`, `has_reduction`) plus the final data model's table/column names.

# Output
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
reduction whose AND/OR semantics you had to infer. This is separate from
the top-level `"notes"` array, which stays for the general RLS-migration
notes already documented above.

# Conversion rules

## USERID-based reduction (most common pattern)
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
that `pbip_build` should materialize a `Security` table in the semantic model
(one row per user/reduction-value pair) so the `LOOKUPVALUE` above resolves.

## Composite reduction (multiple fields, combined)
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

## Admin/override access (bypasses reduction entirely)
If the inline table's `ACCESS` column has a value like `ADMIN` (vs `USER`)
for some rows, and those rows have blank/empty reduction field values (no
`Region`/`Department` etc. — the common way Qlik apps signal "sees
everything"), that role should get **no row restriction at all**, not a
filter that happens to be blank. Emit that role's `table_permissions` with a
filter of `TRUE()` (or omit `table_permissions` for that table entirely) —
never leave it pointing at a `LOOKUPVALUE(...)` that would evaluate to
`BLANK()`/no-match for an admin user, which would silently show them *zero*
rows instead of *all* rows.

## Static OMIT (no per-user dynamic lookup, a fixed field is universally hidden)
If `OMIT` names a field that's simply removed for everyone (not tied to
USERID), that's a **column-level** security concern, not row-level — call
this out under `"notes"` and do not emit a role for it (Power BI Desktop
handles hiding a field structurally, that's a modeling decision, not RLS).

## REDUCTION
Qlik `REDUCTION Field;` clauses reduce the loaded rows themselves, not
per-viewer visibility — if this is the only Section Access mechanism found (no
`USERID` companion), it means the row reduction already happened once at
extract time and there is nothing left to model as RLS; note this explicitly
rather than fabricating a role.

## No Section Access present
When `present` is `false`, return `{"roles": [], "notes": ["No Section Access in source script — no RLS to migrate."]}`.

## Naming
Default role name: `"RestrictedAccess"` unless multiple distinct ACCESS
levels are present in the inline table (e.g. `ADMIN` vs `USER`), in which case
emit one role per distinct ACCESS value, named after that value
(`"Admin"`, `"User"`).
