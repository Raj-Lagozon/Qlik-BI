import { BoxHeader } from "./UploadBox.jsx";
import { downloadUrl } from "../api.js";

export default function DownloadBox({ job, status }) {
  const ready = status === "success";

  return (
    <div className="flex h-full flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <BoxHeader step="3" title="Download" subtitle="Grab the finished project" />

      <div className="mt-4 flex flex-1 flex-col justify-center gap-3">
        <a
          href={ready && job ? downloadUrl(job.jobId, "pbix") : undefined}
          className={`flex items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold shadow-sm transition ${
            ready
              ? "bg-slate-900 text-white hover:bg-slate-800"
              : "cursor-not-allowed bg-slate-100 text-slate-400"
          }`}
          onClick={(e) => !ready && e.preventDefault()}
        >
          <DownloadIcon />
          Download .pbix
        </a>
        <a
          href={ready && job ? downloadUrl(job.jobId, "pbip") : undefined}
          title=".pbip project (zip) — no data embedded, needs a Refresh in Power BI Desktop"
          className={`flex items-center justify-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-semibold transition ${
            ready
              ? "border-slate-300 text-slate-700 hover:border-brand-400 hover:text-brand-700"
              : "cursor-not-allowed border-slate-200 text-slate-300"
          }`}
          onClick={(e) => !ready && e.preventDefault()}
        >
          <DownloadIcon />
          Download .pbip (project)
        </a>
      </div>

      <div className="mt-3 min-h-[2.5rem] text-xs text-slate-500">
        {ready
          ? "Open in Power BI Desktop and click Refresh to load live data."
          : "Available once the job finishes successfully."}
      </div>
    </div>
  );
}

function DownloadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
      <path
        d="M12 4v12m0 0l-4-4m4 4l4-4M5 20h14"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
