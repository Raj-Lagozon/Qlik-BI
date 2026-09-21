import { useCallback, useEffect, useRef, useState } from "react";
import {
  uploadQvf,
  runJob,
  extractJob,
  convertAllJob,
  convertScriptJob,
  convertDataModelJob,
  convertSheetJob,
  buildJob,
  getLogs,
} from "./api.js";

const TERMINAL_STATUSES = ["success", "failed"];
const POLL_MS = 1500;

const STAGE_FN = {
  run: runJob,
  extract: extractJob,
  convert: convertAllJob,
  "convert:script": convertScriptJob,
  "convert:data_model": convertDataModelJob,
  "convert:sheet": convertSheetJob,
  build: buildJob,
};

// One job's state (upload / run-a-stage / poll logs), shared across every
// page so switching pages (Extract / Convert / Build / Components) doesn't
// lose the in-progress job or its log history.
export default function usePipelineJob() {
  const [job, setJob] = useState(null); // { jobId, appName }
  const [status, setStatus] = useState("idle");
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

  const startPolling = useCallback(
    (jobId) => {
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
              setError(data.error || "Job failed — see log above.");
            }
          }
        } catch (err) {
          stopPolling();
          setError(err.message);
        }
      }, POLL_MS);
    },
    [stopPolling]
  );

  const upload = useCallback(async (file) => {
    const data = await uploadQvf(file);
    setJob({ jobId: data.job_id, appName: data.app_name });
    setStatus("uploaded");
    setLines([]);
    setError("");
    offsetRef.current = 0;
  }, []);

  const runStage = useCallback(
    async (stageKey) => {
      if (!job) {
        setError("Upload a .qvf file first.");
        return;
      }
      const fn = STAGE_FN[stageKey];
      if (!fn) return;
      setError("");
      setLines([]);
      offsetRef.current = 0;
      try {
        await fn(job.jobId);
        setStatus("queued");
        startPolling(job.jobId);
      } catch (err) {
        setError(err.message);
      }
    },
    [job, startPolling]
  );

  const isRunning = status === "queued" || status === "running";

  return { job, status, lines, error, isRunning, upload, runStage };
}
