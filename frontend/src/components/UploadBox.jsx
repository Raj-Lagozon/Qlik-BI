import { useRef, useState } from "react";

export default function UploadBox({ job, onUploaded, disabled }) {
  const inputRef = useRef(null);
  const [dragOver, setDragOver] = useState(false);
  const [fileName, setFileName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function handleFile(file) {
    if (!file) return;
    setFileName(file.name);
    setError("");
    setBusy(true);
    try {
      await onUploaded(file);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <BoxHeader step="1" title="Upload" subtitle=".qvf source file" />

      <label
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (!disabled) handleFile(e.dataTransfer.files?.[0]);
        }}
        className={`mt-4 flex flex-1 cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-8 text-center transition ${
          dragOver ? "border-brand-500 bg-brand-50" : "border-slate-300 bg-slate-50"
        } ${disabled ? "cursor-not-allowed opacity-50" : "hover:border-brand-400 hover:bg-brand-50"}`}
      >
        <UploadIcon />
        <span className="mt-2 text-sm font-medium text-slate-700">
          {fileName || "Drop a .qvf file, or click to browse"}
        </span>
        <span className="mt-1 text-xs text-slate-400">Max 500&nbsp;MB</span>
        <input
          ref={inputRef}
          type="file"
          accept=".qvf"
          className="hidden"
          disabled={disabled || busy}
          onChange={(e) => handleFile(e.target.files?.[0])}
        />
      </label>

      <div className="mt-3 min-h-[2.5rem] text-xs">
        {busy && <p className="text-brand-600">Uploading&hellip;</p>}
        {error && <p className="text-red-600">{error}</p>}
        {!busy && !error && job && (
          <p className="text-slate-500">
            App: <span className="font-medium text-slate-700">{job.appName}</span>
          </p>
        )}
      </div>
    </div>
  );
}

export function BoxHeader({ step, title, subtitle }) {
  return (
    <div className="flex items-center gap-2">
      <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-600 text-xs font-bold text-white">
        {step}
      </span>
      <div>
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        <p className="text-xs text-slate-400">{subtitle}</p>
      </div>
    </div>
  );
}

function UploadIcon() {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" className="text-slate-400">
      <path
        d="M12 16V4m0 0L7 9m5-5l5 5M5 20h14"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
