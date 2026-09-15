import { useRef, useState } from "react";

export default function UploadPanel({ onUploaded, disabled }) {
  const inputRef = useRef(null);
  const [fileName, setFileName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function handleUpload() {
    const file = inputRef.current?.files?.[0];
    if (!file) {
      setError("Choose a .qvf file first.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await onUploaded(file);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="row">
      <input
        ref={inputRef}
        type="file"
        accept=".qvf"
        disabled={disabled || busy}
        onChange={(e) => setFileName(e.target.files?.[0]?.name || "")}
      />
      <button onClick={handleUpload} disabled={disabled || busy}>
        {busy ? "Uploading..." : "Upload"}
      </button>
      {fileName && <span className="filename">{fileName}</span>}
      {error && <span className="error">{error}</span>}
    </div>
  );
}
