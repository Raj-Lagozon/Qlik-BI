import StagePage from "../components/StagePage.jsx";

export default function ExtractPage(props) {
  return (
    <StagePage
      {...props}
      step="1"
      title="Extract"
      description="Pull the load script, data model, master measures/dimensions, variables, sheets and Section Access out of the uploaded .qvf."
      actionLabel="Run Extract"
      stageKey="extract"
    />
  );
}
