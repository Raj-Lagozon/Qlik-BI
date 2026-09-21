import StagePage from "../components/StagePage.jsx";
import DownloadBox from "../components/DownloadBox.jsx";

export default function BuildPage(props) {
  const { job, status } = props;
  return (
    <div>
      <StagePage
        {...props}
        step="3"
        title="Build"
        description="Assemble the converted output into a .pbip project and compile a .pbix."
        actionLabel="Run Build"
        stageKey="build"
      />
      {job && status === "success" && (
        <div className="mt-6 max-w-sm">
          <DownloadBox job={job} status={status} />
        </div>
      )}
    </div>
  );
}
