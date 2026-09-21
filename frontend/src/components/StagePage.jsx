import LogPanel from "./LogPanel.jsx";
import { navigate } from "../router.js";

const STATUS_STYLES = {
  idle: "bg-slate-100 text-slate-500",
  uploaded: "bg-slate-100 text-slate-500",
  queued: "bg-amber-100 text-amber-700",
  running: "bg-amber-100 text-amber-700",
  success: "bg-emerald-100 text-emerald-700",
  failed: "bg-red-100 text-red-700",
};

/** Shared page shell for Extract / Convert / Build / Components-* — a
 * title+description, one primary action button, a status pill, and the
 * shared log panel. Every stage page is this same shape, just pointed at a
 * different `stageKey`/label. */
export default function StagePage({
  step,
  title,
  description,
  actionLabel,
  stageKey,
  job,
  status,
  lines,
  error,
  isRunning,
  onRun,
  extra,
}) {
  return (
    <div>
      <div className="mb-6 flex items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-brand-600 text-sm font-bold text-white">
          {step}
        </span>
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">{title}</h1>
          <p className="mt-1 text-sm text-slate-500">{description}</p>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {!job ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center">
          <p className="text-sm text-slate-500">No job yet — upload a .qvf file to get started.</p>
          <button
            onClick={() => navigate("/")}
            className="mt-4 rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700"
          >
            Go to Upload
          </button>
        </div>
      ) : (
        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-slate-500">
              App: <span className="font-medium text-slate-700">{job.appName}</span>
            </p>
            <span
              className={`rounded-full px-3 py-1 text-xs font-medium capitalize ${
                STATUS_STYLES[status] || STATUS_STYLES.idle
              }`}
            >
              {status}
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={() => onRun(stageKey)}
              disabled={isRunning}
              className="flex items-center gap-2 rounded-lg bg-brand-600 px-6 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-brand-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {isRunning ? "Running…" : actionLabel}
            </button>
            {extra}
          </div>
        </div>
      )}

      <LogPanel lines={lines} status={status} />
    </div>
  );
}
