const API = "/api/jobs";

async function asJson(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (${res.status})`);
  }
  return data;
}

export function uploadQvf(file) {
  const form = new FormData();
  form.append("file", file);
  return fetch(`${API}/upload`, { method: "POST", body: form }).then(asJson);
}

export function runJob(jobId) {
  return fetch(`${API}/${jobId}/run`, { method: "POST" }).then(asJson);
}

export function getLogs(jobId, since) {
  return fetch(`${API}/${jobId}/logs?since=${since}`).then(asJson);
}

export function downloadUrl(jobId, kind = "pbix") {
  return `${API}/${jobId}/download/${kind}`;
}
