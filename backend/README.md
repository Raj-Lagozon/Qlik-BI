# Qlik Sense (.qvf) → Power BI backend

FastAPI service that converts a Qlik Sense `.qvf` app into a Power BI
`.pbip`/`.pbix` project: load script, data model, master measures/
dimensions, variables, sheets/visuals, KPI containers and Section Access all
get carried over — driven mostly by an LLM (Azure OpenAI) against three
consolidated skill files, with a thin deterministic layer only where the
LLM's output needs structural assembly (TMDL/PBIR file writing, CSV
partitioning, calendar/aggregate table synthesis).

## Pipeline stages

```
.qvf file
   │  modules/extract  (Qlik Cloud REST import + Engine API / WebSocket JSON-RPC)
   ▼
extracted/<app>/  { script.qvs, data_model.json, measures.json, dimensions.json,
                     variables.json, sheets.json, section_access.json, kpi_containers.json }
   │  modules/script + modules/data_model + modules/sheet  (Azure OpenAI, driven by app/prompts/*.skill.md)
   ▼
converted/<app>/  { m_query__*.m, variables.converted.json, data_model.converted.json,
                     rls.converted.json, measures.converted.json, dimensions.converted.json,
                     page__*.json, kpi_container__*.json, adhoc_expressions.converted.json }
   │  modules/build  (writes TMDL + PBIR, then pbip-compiler packs the .pbix)
   ▼
output/<app>/<app>.pbip  +  <app>.Report/  +  <app>.SemanticModel/  +  <app>.pbix
```

Each stage is also its own API endpoint, so a job can be run incrementally
(useful for debugging one stage without re-running the whole pipeline) or
all at once via `/run`.

## Repo layout

```
backend/
├── app/
│   ├── main.py                 # FastAPI entrypoint
│   ├── routes.py               # aggregates every route_handlers/*.py router
│   ├── setting.py              # env-driven Settings (paths, upload limits, CORS)
│   ├── config.py                # fixed constants (JobStatus, ...)
│   ├── logger.py                # captures pipeline print() output into a job's log
│   ├── exception/                # AppError + subclasses, FastAPI handler registration
│   ├── utilities/
│   │   ├── llm.py                # Azure OpenAI call + skill.md/extracted/converted I/O (shared by every conversion module)
│   │   ├── file_utils.py         # upload validation/save
│   │   └── zip_utils.py          # zips a .pbip project for download
│   ├── prompts/                  # the 3 consolidated skill.md files (see below)
│   ├── modules/
│   │   ├── extract/              # Qlik Cloud client + extractor + section-access parser
│   │   ├── extract_modules.py    # flat facade re-exporting extract_app
│   │   ├── script/                # script.qvs -> M query + variables (LLM, script_conversion.skill.md)
│   │   ├── data_model/            # associative model -> relationships + RLS (LLM, data_model.skill.md)
│   │   ├── sheet/                 # measures / dimensions / visuals / KPI containers (LLM, sheets_convert.skill.md)
│   │   ├── convert_modules.py     # flat facade: convert_script / convert_data_model / convert_sheet / convert_all
│   │   └── build/                 # deterministic TMDL/PBIR/PBIX assembly from converted/<app>/
│   ├── services/                  # job orchestration: extract_step_services, convert_services, build_services, pipeline_services (full run), job_store, step_runner
│   └── route_handlers/            # extract_routes, convert_routes, build_routes, pipeline_routes
└── requirements.txt
```

## Skill files (`app/prompts/`)

Only 3 — one per conversion module, each covering every sub-task that
module's endpoint(s) need (the code tells the LLM which sub-task via a
`"task"` field in the request payload):

- **`script_conversion.skill.md`** — Task A: load script → M query (per
  table). Task B: script/UI variables → Power Query parameters, DAX
  measures, or What-If parameters.
- **`data_model.skill.md`** — Task A: associative model → explicit
  relationships. Task B: Section Access → RLS/OLS roles.
- **`sheets_convert.skill.md`** — Task A: master measures → DAX measures.
  Task B: master dimensions → columns/hierarchies. Task C: sheets →
  PBIR pages/visuals. Task D: KPI-container config tables → KPI cards.

These were consolidated from an earlier 8-file layout and reviewed against
`qlik-bi-components/v1_powerbi-qlik-similarity-components.md` (kept a
sibling of `app/prompts/` for reference — that doc explains the *why*
behind every non-obvious rule in these 3 files).

## API

All endpoints are under `/api/jobs`:

| Method & path | Stage |
|---|---|
| `POST /upload` | upload a `.qvf`, creates a job |
| `POST /{job_id}/extract` | extract only |
| `POST /{job_id}/convert/script` | convert: script.qvs → M query + variables |
| `POST /{job_id}/convert/data-model` | convert: relationships + RLS |
| `POST /{job_id}/convert/sheet` | convert: measures/dimensions/visuals/KPI |
| `POST /{job_id}/convert` | convert: all three, in order |
| `POST /{job_id}/build` | build: converted/ → .pbip + .pbix |
| `POST /{job_id}/run` | the full .qvf → PowerBI pipeline in one call (extract → convert → build) |
| `GET /{job_id}` | job status snapshot |
| `GET /{job_id}/logs?since=N` | poll new log lines |
| `GET /{job_id}/download` / `/download/pbix` | download the compiled `.pbix` |
| `GET /{job_id}/download/pbip` | download the `.pbip` project, zipped |

## Overriding a column's data type

Every column's type is auto-detected (LLM classification + Qlik field-tag
fallback + a build-time reconciliation pass that promotes a text column to
numeric if a measure sums/averages it). If one keeps coming out wrong, drop
a `type_overrides.json` next to that app's extracted data —
`extracted/<app_name>/type_overrides.json`:
```json
{
  "TableName.ColumnName": "string"
}
```
Valid types: `string`, `int64`, `double`, `dateTime`, `boolean`, `variant`.
This is read at `build` time and always wins over auto-detection. It lives
outside `converted/`/`output/`, and re-running `extract` never overwrites
an existing one — so it survives every future re-build/re-extract for that
app until you change or delete it yourself.

## Setup

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env        # fill in Qlik Cloud + Azure OpenAI credentials
uvicorn app.main:app --reload --port 8000
```

(Use `python -m uvicorn app.main:app --reload --port 8000` if `uvicorn` isn't
on PATH.)

Serves the built frontend (`frontend/dist/`) as static files at `/` when
present, alongside the `/api/jobs/*` endpoints above.
