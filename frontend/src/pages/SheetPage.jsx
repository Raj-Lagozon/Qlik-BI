import StagePage from "../components/StagePage.jsx";

export default function SheetPage(props) {
  return (
    <StagePage
      {...props}
      step="C"
      title="Component: Sheet"
      description="Convert master measures, master dimensions, visuals/charts and KPI containers into their Power BI equivalents."
      actionLabel="Run Sheet Conversion"
      stageKey="convert:sheet"
    />
  );
}
