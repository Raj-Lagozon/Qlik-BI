const API = "/api/jobs";

async function asJson(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (${res.status})`);
  }
  return data;
}

function post(path) {
  return fetch(`${API}${path}`, { method: "POST" }).then(asJson);
}

export function uploadQvf(file) {
  const form = new FormData();
  form.append("file", file);
  return fetch(`${API}/upload`, { method: "POST", body: form }).then(asJson);
}

// Full .qvf -> PowerBI pipeline (extract -> convert -> build), kept exactly
// as the original single-button flow always worked.
export function runJob(jobId) {
  return post(`/${jobId}/run`);
}

// Per-stage endpoints (extract only / one convert sub-stage / build only).
export function extractJob(jobId) {
  return post(`/${jobId}/extract`);
}
export function convertScriptJob(jobId) {
  return post(`/${jobId}/convert/script`);
}
export function convertDataModelJob(jobId) {
  return post(`/${jobId}/convert/data-model`);
}
export function convertSheetJob(jobId) {
  return post(`/${jobId}/convert/sheet`);
}
export function convertAllJob(jobId) {
  return post(`/${jobId}/convert`);
}
export function buildJob(jobId) {
  return post(`/${jobId}/build`);
}

export function getJob(jobId) {
  return fetch(`${API}/${jobId}`).then(asJson);
}

export function getLogs(jobId, since) {
  return fetch(`${API}/${jobId}/logs?since=${since}`).then(asJson);
}

export function downloadUrl(jobId, kind = "pbix") {
  return `${API}/${jobId}/download/${kind}`;
}
