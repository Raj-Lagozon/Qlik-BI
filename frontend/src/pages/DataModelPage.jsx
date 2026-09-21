import StagePage from "../components/StagePage.jsx";

export default function DataModelPage(props) {
  return (
    <StagePage
      {...props}
      step="C"
      title="Component: Data Model"
      description="Convert Qlik's associative data model into explicit Power BI relationships, plus Section Access into RLS/OLS roles."
      actionLabel="Run Data Model Conversion"
      stageKey="convert:data_model"
    />
  );
}
