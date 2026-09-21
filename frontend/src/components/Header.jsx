import { useEffect, useRef, useState } from "react";
import { navigate } from "../router.js";

const COMPONENT_ITEMS = [
  { path: "/components/script", label: "Script" },
  { path: "/components/sheet", label: "Sheet" },
  { path: "/components/data_model", label: "Data Model" },
];

export default function Header({ route }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    function onClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  const isComponentsActive = route.startsWith("/components/");

  return (
    <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
        <button
          onClick={() => navigate("/")}
          className="flex items-center gap-2"
        >
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-brand-700 text-sm font-bold text-white">
            Q→P
          </div>
          <span className="text-lg font-semibold tracking-tight text-slate-900">
            Qlik <span className="text-brand-600">&rarr;</span> Power BI
          </span>
        </button>

        <nav className="flex items-center gap-1 text-sm font-medium text-slate-600">
          <NavLink to="/extract" active={route === "/extract"}>
            Extract
          </NavLink>
          <NavLink to="/convert" active={route === "/convert"}>
            Convert
          </NavLink>
          <NavLink to="/build" active={route === "/build"}>
            Build
          </NavLink>

          <div className="relative" ref={ref}>
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              className={`flex items-center gap-1 rounded-md px-3 py-2 hover:bg-slate-100 hover:text-slate-900 ${
                isComponentsActive ? "bg-brand-50 text-brand-700" : ""
              }`}
            >
              Components
              <svg width="12" height="12" viewBox="0 0 12 12" className={`transition-transform ${open ? "rotate-180" : ""}`}>
                <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            {open && (
              <div className="absolute right-0 mt-1 w-44 overflow-hidden rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
                {COMPONENT_ITEMS.map((item) => (
                  <button
                    key={item.path}
                    onClick={() => {
                      setOpen(false);
                      navigate(item.path);
                    }}
                    className={`block w-full px-4 py-2 text-left text-sm hover:bg-brand-50 hover:text-brand-700 ${
                      route === item.path ? "bg-brand-50 font-medium text-brand-700" : "text-slate-700"
                    }`}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </nav>
      </div>
    </header>
  );
}

function NavLink({ to, children, active }) {
  return (
    <button
      type="button"
      onClick={() => navigate(to)}
      className={`rounded-md px-3 py-2 hover:bg-slate-100 hover:text-slate-900 ${
        active ? "bg-brand-50 text-brand-700" : ""
      }`}
    >
      {children}
    </button>
  );
}
