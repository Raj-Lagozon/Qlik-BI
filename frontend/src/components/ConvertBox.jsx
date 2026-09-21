import { BoxHeader } from "./UploadBox.jsx";

const STATUS_STYLES = {
  idle: "bg-slate-100 text-slate-500",
  uploaded: "bg-slate-100 text-slate-500",
  queued: "bg-amber-100 text-amber-700",
  running: "bg-amber-100 text-amber-700",
  success: "bg-emerald-100 text-emerald-700",
  failed: "bg-red-100 text-red-700",
};

export default function ConvertBox({ job, status, onRun, disabled }) {
  const isRunning = status === "queued" || status === "running";

  return (
    <div className="flex h-full flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <BoxHeader step="2" title="Convert" subtitle="Run the full .qvf → PowerBI pipeline" />

      <div className="mt-4 flex flex-1 flex-col items-center justify-center gap-4 rounded-lg bg-slate-50 px-4 py-8">
        <button
          onClick={onRun}
          disabled={disabled || isRunning}
          className="flex items-center gap-2 rounded-lg bg-brand-600 px-6 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-brand-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {isRunning ? <Spinner /> : <PlayIcon />}
          {isRunning ? "Running…" : "Run Pipeline"}
        </button>

        <span
          className={`rounded-full px-3 py-1 text-xs font-medium capitalize ${
            STATUS_STYLES[status] || STATUS_STYLES.idle
          }`}
        >
          {status === "idle" ? "no job yet" : status}
        </span>
      </div>

      <div className="mt-3 min-h-[2.5rem] text-xs text-slate-500">
        {job ? (
          <>
            extract &rarr; convert (script / data-model / sheet) &rarr; build — or use the header nav for
            individual stages.
          </>
        ) : (
          "Upload a .qvf file first."
        )}
      </div>
    </div>
  );
}

function PlayIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

function Spinner() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" className="animate-spin">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.25" />
      <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" />
    </svg>
  );
}
