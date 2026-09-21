import StagePage from "../components/StagePage.jsx";
import { navigate } from "../router.js";

export default function ConvertPage(props) {
  return (
    <StagePage
      {...props}
      step="2"
      title="Convert"
      description="Run every conversion stage — script, data model, and sheet — against the extracted app, in order."
      actionLabel="Run Full Convert"
      stageKey="convert"
      extra={
        <div className="flex flex-wrap items-center gap-2 border-l border-slate-200 pl-3 text-xs text-slate-500">
          <span>or run one stage:</span>
          <PageLink to="/components/script">Script</PageLink>
          <PageLink to="/components/sheet">Sheet</PageLink>
          <PageLink to="/components/data_model">Data Model</PageLink>
        </div>
      }
    />
  );
}

function PageLink({ to, children }) {
  return (
    <button
      onClick={() => navigate(to)}
      className="rounded-md border border-slate-200 px-2.5 py-1 font-medium text-slate-600 hover:border-brand-400 hover:text-brand-700"
    >
      {children}
    </button>
  );
}
