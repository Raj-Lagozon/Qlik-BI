# Qlik → Power BI frontend

React (Vite) + Tailwind CSS UI for the [backend](../backend/README.md)'s
per-stage and full-pipeline job API.

## Layout

- **Header** — nav: `Extract`, `Convert`, `Build` (each triggers the
  matching standalone job stage), and a `Components` dropdown
  (`Script` / `Sheet` / `Data Model` — triggers `convert/script`,
  `convert/sheet`, `convert/data-model` respectively).
- **Main row** — three cards: **Upload** (drag-and-drop `.qvf`), **Convert**
  (runs the full `.qvf` → PowerBI pipeline via `/run`, kept as the simple
  one-click flow), **Download** (`.pbix` / `.pbip` project zip).
- **Logs panel** — streams the running job's console output (polls
  `/{job_id}/logs` every 1.5s), below the main row.

```
src/
├── App.jsx                 # job state machine: upload -> run/stage -> poll logs -> download
├── api.js                  # fetch wrappers for every /api/jobs/* endpoint
├── components/
│   ├── Header.jsx           # nav + Components dropdown
│   ├── UploadBox.jsx        # drag-and-drop upload card (also exports BoxHeader)
│   ├── ConvertBox.jsx       # full-pipeline run button + status pill
│   ├── DownloadBox.jsx      # .pbix / .pbip download buttons
│   └── LogPanel.jsx         # dark terminal-style log viewer, auto-scrolls
└── index.css                # Tailwind directives
```

## Setup

```bash
npm install
npm run dev          # http://127.0.0.1:5173, proxies /api to http://127.0.0.1:8000
```

```bash
npm run build         # writes dist/, served by the backend at / in production
```

`vite.config.js` proxies `/api/*` to the backend during `npm run dev` so the
same relative `fetch("/api/...")` calls used in production work in dev too
— no `.env` is required unless the backend runs somewhere other than
`127.0.0.1:8000` (see `.env.example`).
