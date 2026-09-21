import UploadBox from "../components/UploadBox.jsx";
import ConvertBox from "../components/ConvertBox.jsx";
import DownloadBox from "../components/DownloadBox.jsx";
import LogPanel from "../components/LogPanel.jsx";

export default function OverviewPage({ job, status, lines, error, isRunning, upload, runStage }) {
  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">
          Qlik Sense &rarr; Power BI Migration
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Upload a <code className="rounded bg-slate-100 px-1 py-0.5">.qvf</code> app, run the full pipeline
          here, or use the header nav for individual stages (Extract / Convert / Build / Components).
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <UploadBox job={job} onUploaded={upload} disabled={isRunning} />
        <ConvertBox job={job} status={status} onRun={() => runStage("run")} disabled={!job} />
        <DownloadBox job={job} status={status} />
      </div>

      <LogPanel lines={lines} status={status} />
    </div>
  );
}
