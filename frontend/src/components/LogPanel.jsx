import { useEffect, useRef } from "react";

export default function LogPanel({ lines }) {
  const boxRef = useRef(null);

  useEffect(() => {
    if (boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [lines]);

  return (
    <div className="log" ref={boxRef}>
      {lines.length === 0 ? (
        <span className="log-placeholder">Logs will appear here once you click Run.</span>
      ) : (
        lines.join("\n")
      )}
    </div>
  );
}
