import { useEffect, useState } from "react";

// Tiny hash-based router — no extra dependency needed for a handful of
// pages. "#/extract" etc. so pages are directly linkable/bookmarkable and
// the browser's back/forward buttons work.

function currentPath() {
  const hash = window.location.hash.replace(/^#/, "");
  return hash || "/";
}

export function navigate(path) {
  if (currentPath() !== path) {
    window.location.hash = path;
  }
}

export function useRoute() {
  const [path, setPath] = useState(currentPath());

  useEffect(() => {
    function onHashChange() {
      setPath(currentPath());
    }
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return path;
}
