# Qlik Sense (.qvf) → Power BI (.pbix) pipeline

Converts a Qlik Sense app into a Power BI project with the same KPIs and
charts: load script, data model, master measures/dimensions, variables,
sheets/visuals and Section Access all get carried over.

## How it works

```
.qvf file
   │  qlik_extract  (Qlik Cloud REST import + Engine API / WebSocket JSON-RPC)
   ▼
extracted/<app>/  { script.qvs, data_model.json, measures.json, dimensions.json,
                     variables.json, sheets.json, section_access.json,
                     data/<table>.csv  <- real exported rows, one CSV per table }
   │  llm_convert  (Azure OpenAI, one call per skills/*.skill.md)
   ▼
converted/<app>/  { m_query__*.m, data_model.converted.json, measures.converted.json,
                     dimensions.converted.json, variables.converted.json,
                     page__*.json, rls.converted.json }
   │  pbip_build   (writes TMDL + PBIR, then pbip-compiler packs the .pbix)
   ▼
output/<app>/<app>.pbip  +  <app>.Report/  +  <app>.SemanticModel/  +  <app>.pbix
```

**Data loading:** each table's partition M loads its `extracted/<app>/data/<table>.csv`
directly (an absolute local path baked into the M) rather than trying to
reconstruct the Qlik app's original source (a file share, DB, or `lib://`
connection only the Qlik author's machine can reach). The CSV holds the
table's actual rows, exported straight from Qlik via the Engine API — so
Refresh always works, using data that's already faithful to the Qlik app,
without you needing to fix any connection strings. The `m_query.skill.md`
LLM conversion of the original LOAD/RESIDENT/CROSSTABLE script still runs and
is kept under `converted/<app>/m_query__*.m` for documentation of the
original transform logic, but the CSV-backed load always wins when both
exist.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in QLIK_TENANT_URL, QLIK_API_KEY, AZURE_OPENAI_*
```

## Usage

```bash
# one shot: .qvf straight to a finished .pbix
python cli.py run-all path\to\MyApp.qvf

# or step by step
python cli.py extract path\to\MyApp.qvf --name MyApp
python cli.py convert MyApp
python cli.py build MyApp
```

Output lands in `output/MyApp/MyApp.pbix` — double-click it to open in Power
BI Desktop.

**Important: click Refresh after opening.** The compiled `.pbix` embeds one
placeholder row per table so the file is valid the moment it's built; the
real rows load when Power BI runs each table's generated Power Query (M)
step. Home ribbon → **Refresh**.

## Confidence flags

Every LLM conversion call returns a `"confidence": "high"|"medium"|"low"`
per item (per measure, per relationship, per visual, ...) — the model's own
judgment of how certain that specific translation is, separate from and in
addition to the deterministic code-level checks in `pbip_build` (hallucinated
table names, unresolved measures, relationship cycles, etc). `python cli.py
convert <app>` prints a batch summary at the end:
```
[convert] confidence flags: 2 medium, 1 low — review before trusting the build
[convert]   [LOW] dax_measures: 'Rolling Avg' — Above() needs pivot sort context not fully available
```
A low-confidence item can still build and render fine — this is a review
flag, not a build blocker. Check the flagged items in the corresponding
`converted/<app>/*.converted.json` file (or, for `m_query`, a
`// CONFIDENCE:` comment line the builder already stripped out of the
`.m` file — the reason is in the same console summary) before trusting that
one specific number/chart. Calls with nothing to convert (no measures, no
dimensions, no variables, no Section Access) are skipped entirely rather
than making a wasted LLM round-trip — you'll see a
`[convert] no ... — skipping ... LLM call` line for those.

## The 8 conversion domains and where they live

| # | Qlik concept | Target | Skill file |
|---|---|---|---|
| 2.1 | Load script (LOAD/RESIDENT/CROSSTABLE) | Power Query M | `skills/m_query.skill.md` |
| 2.2 | Field associations | Relationships, data types | `skills/data_model.skill.md` |
| 2.3 | Master measures (set analysis, Sum/Count) | DAX measures | `skills/dax_measures.skill.md` |
| 2.4 | Master dimensions & drill-downs | DAX calculated columns & hierarchies | `skills/dax_columns_hierarchies.skill.md` |
| 2.5 | Variables (vMaxYear, vCurrency) | PQ parameters or DAX measures | `skills/parameters_variables.skill.md` |
| 2.6/2.7 | Sheets, visuals, x/y/w/h layout | Report pages & PBIR visual JSON | `skills/report_visuals.skill.md` |
| 2.8 | Section Access (USERID/OMIT/REDUCTION) | Row-Level Security roles | `skills/rls_section_access.skill.md` |
| — | Qlik "KPI container" config tables (a table of title/measure/color rows driving N generic KPI tiles) | Individual KPI card visuals | `skills/kpi_container.skill.md` |

## What-if sliders

A Qlik "variable input" slider (the `qlik-variable-input` extension — a
control that lets the user manually drive a variable within a numeric
range, e.g. "Sales Achievement %" from 50–150) is detected directly from
`sheets.json` (`pbip_build/what_if_params.py`, no LLM call needed — the
slider's own properties already carry `variableName`/`min`/`max`/`step`)
and turned into a real Power BI equivalent: a small table holding the
numeric range plus a `SELECTEDVALUE()` measure **named after the slider's
own display label**. Since other measures reference that label directly
(e.g. `[Sales Achievement %]`), they resolve automatically — no DAX
rewriting needed elsewhere. Add a slicer visual bound to that table's
`Value` column in Power BI Desktop to get back the interactive slider.

## Relationships and KPI containers

**Table relationships** are derived two ways and merged: the LLM's read of
Qlik's own association metadata (`data_model.skill.md`), and a deterministic
pass (`pbip_build/infer_relationships.py`) that compares actual column names
and measures each candidate key's uniqueness directly against the *real*
exported CSV data. The data-driven pass wins when both find the same table
pair, since it's grounded in actual row data rather than depending on Qlik
Engine API metadata whose exact behavior can vary by tenant/version.

**KPI containers** come in two different shapes, both handled:
- A **config-table-driven** container: one table (columns like
  `Title`/`Measure`/`Bg Color`) read at runtime by a generic extension to
  render N KPI tiles, instead of each KPI being its own chart object.
  `qlik_extract` detects such a table (heuristic: a "title" field alongside
  a "measure" field), `kpi_container.skill.md` resolves each row's measure
  reference (or synthesizes a new DAX measure), and `pbip_build` turns each
  row into a real card visual on a dedicated page, hiding the raw config
  table (it's plumbing, not something to browse). Complex per-row
  conditional coloring isn't convertible to a static color and is left as a
  note for manual Power BI conditional formatting.
- A **native Qlik container object** (`sn-layout-container`): Qlik's
  standard "group several tiles together" object. Its real children (often
  KPI cards) aren't visible to the normal per-sheet-object extraction at
  all — only the empty container is. `qlik_extract._expand_container` (in
  `qlik_extract/extractor.py`) recurses into it via the Engine API's
  `GetChildInfos` method and pulls each child's own chart data, auto-laying
  them out in a grid inside the container's bounds (the Engine API doesn't
  expose each child's exact position, only its id/type). **This path
  couldn't be verified against a live Qlik Cloud tenant while building it**
  — re-run `extract` and check the console for
  `[extract] WARNING: could not expand container ...` to confirm it worked;
  if `GetChildInfos`' response shape differs from what's assumed, report the
  exact warning text so the parsing can be corrected.

## Known limitations (pbip-compiler v0.2.0, alpha)

- **The .pbix only refreshes on the machine that ran `extract`/`build`** —
  the M's `File.Contents(...)` path points at this project's local
  `extracted/<app>/data/*.csv` files by absolute path. Moving or sharing the
  `.pbix` alone will make Refresh fail (the tables will still show whatever
  was last refreshed, since the placeholder-then-refresh data isn't
  embedded until you refresh once). To hand off a self-contained file, use
  Power BI Desktop's **Transform data → Data source settings** to repoint
  each query at a copied CSV, or publish to the Power BI service after
  refreshing locally.
- **Visual-level Top N filters aren't carried into the compiled `.pbix`** —
  Qlik's "limit to top/bottom N" dimension setting is captured in
  `extracted/<app>/sheets.json`, but `pbip-compiler`'s PBIR→legacy filter
  conversion doesn't yet forward `howMany`/direction parameters, so a
  Top 5 / Bottom 5 table converts as an unfiltered table. Add the filter
  manually in Power BI Desktop (Filters pane → Filter type: Top N) for any
  visual that used this in Qlik.
- Two sheets with the same or very similar name (e.g. "Incentive management"
  and "Incentive management (1)") convert as two separate report pages,
  faithfully — that's not deduped since they're genuinely different sheets in
  the source Qlik app.
- Very large tables are capped at 500,000 exported rows (`MAX_ROWS_PER_TABLE`
  in `qlik_extract/extractor.py`) as a safety limit — raise it if your app
  has bigger tables.
- **Calculated columns, hierarchies and RLS roles are written to the
  `.pbip`/TMDL project but are not baked into the compiled `.pbix`** — this
  version of `pbip-compiler` only carries tables, plain columns, measures and
  relationships into the VertiPaq model it builds. To get those three things
  into a working file, open the generated `<app>.pbip` directly in Power BI
  Desktop (not the `.pbix`) — Desktop's own TMDL loader supports the full
  spec — and save as `.pbix` from there.
- Power Query **parameters** (item 2.5) are written as a reference file
  (`<app>.SemanticModel/parameters.pq.txt`) rather than live PBIP parameter
  objects, since neither TMDL-parsed-by-pbip-compiler nor a hand-rolled PBIP
  parameter format was safe to guess without testing against Power BI
  Desktop directly — add them via Home → Manage Parameters using that file as
  the spec.
- The Qlik→Power BI visual type/layout mapping (`report_visuals.skill.md`) is
  a best-effort table; unusual or custom Qlik extension objects fall back to
  a text box noting the original type so nothing is silently dropped.

## Validating parity

Open the source `.qvf` in Qlik Sense and the generated `.pbix` in Power BI
Desktop side by side; compare KPI values and chart types/layout per page.
