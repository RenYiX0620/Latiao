import CloudModelsTab from "./CloudModelsTab";
import LocalModelsTab from "./LocalModelsTab";
import type { CloudModel, HFModelResult, DownloadState, SetupIssue, LLMStatus } from "../types";

interface ModelsViewProps {
  modelTab: "cloud" | "local";
  selectedModel: string;
  setSelectedModel: (m: string) => void;
  cloudModels: CloudModel[];
  setCloudModels: React.Dispatch<React.SetStateAction<CloudModel[]>>;
  newCloudModel: CloudModel;
  setNewCloudModel: React.Dispatch<React.SetStateAction<CloudModel>>;
  showAdvanced: boolean;
  setShowAdvanced: (v: boolean) => void;
  testingModel: string | null;
  testResult: string;
  testConnection: (modelName: string, key: string, endpoint: string, protocol: string) => void;
  recentLearnings: { topic: string; content: string; confidence: number }[];
  localLLMStatus: LLMStatus;
  localModelId: string;
  setLocalModelId: (id: string) => void;
  setupCheck: { ready: boolean; ok: { item: string; status: string }[]; issues: SetupIssue[] } | null;
  hfSearch: string;
  setHfSearch: (s: string) => void;
  hfResults: HFModelResult[];
  searching: boolean;
  searchHF: (query?: string) => void;
  downloadProgress: Record<string, DownloadState>;
  downloadModel: (modelId: string) => void;
  pauseDownload: (modelId: string) => void;
  resumeDownload: (modelId: string) => void;
  cancelDownload: (modelId: string) => void;
  startLocalLLM: (modelId?: string) => void;
  stopLocalLLM: () => void;
  fixing: string;
  runFix: (fixType: string, fixPkg: string) => void;
  showToast: (msg: string, type?: string) => void;
}

export default function ModelsView(props: ModelsViewProps) {
  const cloudProps = {
    selectedModel: props.selectedModel,
    setSelectedModel: props.setSelectedModel,
    cloudModels: props.cloudModels,
    setCloudModels: props.setCloudModels,
    newCloudModel: props.newCloudModel,
    setNewCloudModel: props.setNewCloudModel,
    showAdvanced: props.showAdvanced,
    setShowAdvanced: props.setShowAdvanced,
    testingModel: props.testingModel,
    testResult: props.testResult,
    testConnection: props.testConnection,
    recentLearnings: props.recentLearnings,
    showToast: props.showToast,
  };

  const localProps = {
    localLLMStatus: props.localLLMStatus,
    localModelId: props.localModelId,
    setLocalModelId: props.setLocalModelId,
    setupCheck: props.setupCheck,
    hfSearch: props.hfSearch,
    setHfSearch: props.setHfSearch,
    hfResults: props.hfResults,
    searching: props.searching,
    searchHF: props.searchHF,
    downloadProgress: props.downloadProgress,
    downloadModel: props.downloadModel,
    pauseDownload: props.pauseDownload,
    resumeDownload: props.resumeDownload,
    cancelDownload: props.cancelDownload,
    startLocalLLM: props.startLocalLLM,
    stopLocalLLM: props.stopLocalLLM,
    fixing: props.fixing,
    runFix: props.runFix,
    showToast: props.showToast,
  };

  return (
    <div className="page-body">
      {props.modelTab === "cloud" ? (
        <CloudModelsTab {...cloudProps} />
      ) : (
        <LocalModelsTab {...localProps} />
      )}
    </div>
  );
}
