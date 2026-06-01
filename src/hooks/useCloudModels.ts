import { useState, useEffect, useCallback } from "react";
import { fetch } from "@tauri-apps/plugin-http";
import type { CloudModel } from "../types";
import { encrypt, decrypt } from "../utils/crypto";

const SIDECAR = "http://127.0.0.1:8000";
const ENC_KEY = "latiao…_enc";
const OLD_KEY = "local_…dels";

export function useCloudModels(showToast: (msg: string, type?: string) => void) {
  const [cloudModels, setCloudModels] = useState<CloudModel[]>([]);
  const [cloudModelsLoaded, setCloudModelsLoaded] = useState(false);
  const [newCloudModel, setNewCloudModel] = useState<CloudModel>({
    name: "", key: "", endpoint: "", protocol: "openai", max_tokens: 32768,
  });
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [testingModel, setTestingModel] = useState<string | null>(null);
  const [testResult, setTestResult] = useState("");

  // Load encrypted cloud models on first mount
  useEffect(() => {
    (async () => {
      try {
        const enc = localStorage.getItem(ENC_KEY);
        if (enc) {
          setCloudModels(JSON.parse(await decrypt(enc)));
          setCloudModelsLoaded(true);
          return;
        }
        const old = localStorage.getItem(OLD_KEY);
        if (old) {
          const models = JSON.parse(old);
          setCloudModels(models);
          localStorage.setItem(ENC_KEY, await encrypt(old));
          localStorage.removeItem(OLD_KEY);
        }
      } catch { /* ignore */ }
      setCloudModelsLoaded(true);
    })();
  }, []);

  // Persist encrypted
  useEffect(() => {
    if (!cloudModelsLoaded) return;
    (async () => {
      try {
        localStorage.setItem(ENC_KEY, await encrypt(JSON.stringify(cloudModels)));
        localStorage.removeItem(OLD_KEY);
      } catch { /* ignore */ }
    })();
  }, [cloudModels, cloudModelsLoaded]);

  const testConnection = useCallback(async (modelName: string, key: string, endpoint: string, protocol: string) => {
    if (!modelName || !key) { showToast("请先填写模型 ID 和 API Key"); return; }
    setTestingModel(modelName);
    setTestResult("");
    try {
      const resp = await fetch(SIDECAR + "/v1/test_connection", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelName, key, endpoint, protocol }),
      });
      const data = await resp.json();
      setTestResult(data.status === "ok" ? "✅ " + data.message : "❌ " + (data.message || "连接失败"));
    } catch { setTestResult("❌ 无法连接 Sidecar"); }
    finally { setTestingModel(null); }
  }, [showToast]);

  return {
    cloudModels, setCloudModels,
    newCloudModel, setNewCloudModel,
    showAdvanced, setShowAdvanced,
    testingModel, testResult,
    testConnection,
  };
}
