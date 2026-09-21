import Header from "./components/Header.jsx";
import { useRoute } from "./router.js";
import usePipelineJob from "./usePipelineJob.js";
import OverviewPage from "./pages/OverviewPage.jsx";
import ExtractPage from "./pages/ExtractPage.jsx";
import ConvertPage from "./pages/ConvertPage.jsx";
import BuildPage from "./pages/BuildPage.jsx";
import ScriptPage from "./pages/ScriptPage.jsx";
import SheetPage from "./pages/SheetPage.jsx";
import DataModelPage from "./pages/DataModelPage.jsx";

const PAGES = {
  "/": OverviewPage,
  "/extract": ExtractPage,
  "/convert": ConvertPage,
  "/build": BuildPage,
  "/components/script": ScriptPage,
  "/components/sheet": SheetPage,
  "/components/data_model": DataModelPage,
};

export default function App() {
  const route = useRoute();
  const pipeline = usePipelineJob();

  const Page = PAGES[route] || OverviewPage;

  return (
    <div className="min-h-screen">
      <Header route={route} />
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Page {...pipeline} onRun={pipeline.runStage} />
      </main>
    </div>
  );
}
