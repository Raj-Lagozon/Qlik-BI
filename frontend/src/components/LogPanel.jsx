import { useEffect, useRef } from "react";

export default function LogPanel({ lines, status }) {
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lines]);

  return (
    <div className="mt-6 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-5 py-3">
        <h2 className="text-sm font-semibold text-slate-900">Conversion logs</h2>
        <span className="text-xs text-slate-400">{lines.length} line{lines.length === 1 ? "" : "s"}</span>
      </div>
      <div className="h-80 overflow-y-auto bg-slate-950 px-5 py-4 font-mono text-[12.5px] leading-relaxed text-slate-200">
        {lines.length === 0 ? (
          <p className="text-slate-500">
            {status === "idle" ? "Logs will stream here once a job runs." : "Waiting for output…"}
          </p>
        ) : (
          lines.map((line, i) => (
            <div key={i} className={lineClass(line)}>
              {line}
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function lineClass(line) {
  if (/error/i.test(line)) return "text-red-400";
  if (/warning/i.test(line)) return "text-amber-400";
  if (/^\[convert\]\s+===/.test(line)) return "mt-2 text-brand-300 font-semibold";
  return "text-slate-200";
}
