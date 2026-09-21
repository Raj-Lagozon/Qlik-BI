# Qlik Sense (.qvf) → Power BI

Converts a Qlik Sense app into a Power BI `.pbip`/`.pbix` project — load
script, data model, master measures/dimensions, variables, sheets/visuals,
KPI containers and Section Access all get carried over, driven mostly by an
LLM (Azure OpenAI) rather than hand-written rules.

This is a web app: **[backend/](backend/README.md)** (FastAPI, per-stage +
full-pipeline endpoints) and **[frontend/](frontend/README.md)** (React +
Tailwind — upload/convert/download UI with live conversion logs). See each
folder's own README for setup, the API surface, and the pipeline's stage
breakdown (extract → convert (script / data-model / sheet) → build).

```bash
# backend
cd backend
pip install -r requirements.txt
cp .env.example .env        # fill in Qlik Cloud + Azure OpenAI credentials
uvicorn app.main:app --reload --port 8000

# frontend (separate terminal)
cd frontend
npm install
npm run dev                 # http://127.0.0.1:5173, proxies /api to :8000
```

`extracted/`, `converted/`, and `output/` (pipeline artifacts) are
gitignored — every run's intermediate/final files land there under
`<stage>/<app_name>/`.
