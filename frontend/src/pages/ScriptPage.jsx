import StagePage from "../components/StagePage.jsx";

export default function ScriptPage(props) {
  return (
    <StagePage
      {...props}
      step="C"
      title="Component: Script"
      description="Convert the Qlik load script (script.qvs) into Power Query M partitions and variables/parameters."
      actionLabel="Run Script Conversion"
      stageKey="convert:script"
    />
  );
}
