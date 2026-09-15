import { useCallback, useEffect, useRef, useState } from "react";
import UploadPanel from "./components/UploadPanel.jsx";
import LogPanel from "./components/LogPanel.jsx";
import { uploadQvf, runJob, getLogs, downloadUrl } from "./api.js";

const TERMINAL_STATUSES = ["success", "failed"];
const POLL_MS = 1500;

export default function App() {
  const [job, setJob] = useState(null); // { jobId, appName }
  const [status, setStatus] = useState("idle"); // idle | uploaded | queued | running | success | failed
  const [lines, setLines] = useState([]);
  const [error, setError] = useState("");
  const offsetRef = useRef(0);
  const pollRef = useRef(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  async function handleUploaded(file) {
    const data = await uploadQvf(file);
    setJob({ jobId: data.job_id, appName: data.app_name });
    setStatus("uploaded");
    setLines([]);
    setError("");
    offsetRef.current = 0;
  }

  async function handleRun() {
    if (!job) return;
    setError("");
    try {
      await runJob(job.jobId);
      setStatus("queued");
      startPolling(job.jobId);
    } catch (err) {
      setError(err.message);
    }
  }

  function startPolling(jobId) {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const data = await getLogs(jobId, offsetRef.current);
        if (data.lines.length) {
          setLines((prev) => [...prev, ...data.lines]);
        }
        offsetRef.current = data.next_offset;
        setStatus(data.status);
        if (TERMINAL_STATUSES.includes(data.status)) {
          stopPolling();
          if (data.status === "failed") {
            setError(data.error || "Pipeline failed — see log above.");
          }
        }
      } catch (err) {
        stopPolling();
        setError(err.message);
      }
    }, POLL_MS);
  }

  const isRunning = status === "queued" || status === "running";

  return (
    <div className="page">
      <h1>Qlik (.qvf) &rarr; Power BI (.pbix) Converter</h1>

      <UploadPanel onUploaded={handleUploaded} disabled={isRunning} />

      <div className="row">
        <button onClick={handleRun} disabled={!job || isRunning}>
          {isRunning ? "Running..." : "Run"}
        </button>
        <button
          className="secondary"
          disabled={status !== "success"}
          onClick={() => job && (window.location.href = downloadUrl(job.jobId, "pbix"))}
        >
          Download .pbix
        </button>
        <button
          className="secondary"
          disabled={status !== "success"}
          onClick={() => job && (window.location.href = downloadUrl(job.jobId, "pbip"))}
          title=".pbip project (zip) — needs a Refresh in Power BI Desktop, has no data embedded"
        >
          Download .pbip (project)
        </button>
      </div>

      <div className="status">
        {job ? (
          <>
            App: <strong>{job.appName}</strong> &middot; Status: <strong>{status}</strong>
          </>
        ) : (
          "No file uploaded."
        )}
        {error && <div className="error">{error}</div>}
      </div>

      <LogPanel lines={lines} />
    </div>
  );
}
