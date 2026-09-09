---
name: qlik-variables-to-powerbi
description: Convert Qlik script/UI variables (vMaxYear, vCurrency, ...) into Power Query parameters or DAX measures depending on how they're used
---

# Role
Qlik variables serve two different purposes that need two different Power BI
equivalents. You decide which one applies per variable and produce the
matching artifact.

# Critical: preserve exact names
Every variable name and table/column name in your output must be **copied
verbatim** from the input JSON — same spelling, same casing (including
Qlik's own punctuation, e.g. a trailing `%` in `vSalesAchievement%`). Never
invent a name, never normalize/clean one up, and never substitute a name
from this document's own illustrative examples (`vMaxYear`, `vCurrency`,
`Sales`, etc. are placeholders explaining the *pattern*, not real names to
fall back on). A renamed variable breaks silently — any other measure that
references it by its original name (via `[vVarName]` bracket substitution)
will fail to resolve.

# Input you receive
The full `variables` array from `variables.json` (`name`, `definition`,
`comment`, `is_script_created` per item) plus `script` (the full load script,
so you can see where else each variable name appears) — used to decide each
variable's role.

# Output
Convert every item in the input array. Return:
```json
{"variables": [
  {
    "name": "vMaxYear",
    "target": "power_query_parameter" | "dax_measure",
    "table": "<owning table, dax_measure target only>",
    "power_query": {"type": "Number.Type|Text.Type|Date.Type", "current_value": "..."},
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

# Decision rule
- **Power Query parameter** when the variable holds a *static or load-time*
  value used only inside the load script (referenced inside `LOAD`/`WHERE`
  clauses, connection strings, or file paths) — e.g. `vCurrency` set once as
  `'USD'` and used in a script `WHERE Currency = '$(vCurrency)'`.
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

# Conversion rules

## Power Query parameter
- Infer M type from the variable's literal value: numeric literal → `Number.Type`,
  quoted string → `Text.Type`, `Date/Now()/Today()` calls → `Date.Type`.
- `current_value` is the literal, unwrapped of Qlik quoting, e.g. Qlik
  `vCurrency: 'USD'` → `current_value: "USD"`.
- Any M query (from the m_query skill) that referenced this variable name as
  a bare identifier will resolve against this parameter automatically once
  it's declared in the Power Query `Parameters` group — no further wiring
  needed from this skill.

## DAX measure
- `vMaxYear: =Max(Year)` → `expression: "CALCULATE(MAX(Sales[Year]), ALL(Sales[Year]))"`
  (use `ALL` on the relevant table so the "max" ignores the current filter
  context, matching Qlik's variable-computed-once-then-reused semantics,
  unless the variable is meant to react to selections — check the comment/
  usage context to decide whether to wrap with `ALL`).
- Mark these hidden (`is_hidden: true` in the resulting TMDL measure) unless
  the variable is also directly displayed as a KPI somewhere in the app.

## Interactive UI-triggered variable (slider / input box), not just script-time
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

## Variable holding a full set-analysis expression (`$(=vFilter)` usage)
Some variables store an entire set-analysis clause as their definition, not
a scalar value — e.g. `vFilter: ={<Status={'Active'}, Region={'$(vRegion)'}>}`
— then get spliced into other measures as `Sum({$(vFilter)} Amount)`. This
is different from the `vMaxYear`-style "aggregation variable" case: the
variable's *text* is itself a set-analysis condition, so it can't become one
self-contained DAX measure — inline the resolved filter conditions directly
into whatever measure references it, the same way `dax_measures.skill.md`
converts an inline set-analysis clause:
- `vFilter: ={<Status={'Active'}, Region={'$(vRegion)'}>}` used in
  `Sum({$(vFilter)} Amount)` → when converting THAT measure (in
  `dax_measures.skill.md`'s pass), resolve `vFilter`'s definition first and
  inline its conditions: `CALCULATE(SUM(Sales[Amount]), Sales[Status] = "Active", Sales[Region] = <vRegion's current value>)`.
- For this skill's own output, emit `"target": "dax_measure"` with
  `"dax": {"expression": null}` and a `"notes"` entry explaining this
  variable is a set-analysis fragment meant to be inlined into other
  measures, not evaluated standalone — so the builder doesn't try to create
  a real measure from a expression that isn't valid DAX on its own.
