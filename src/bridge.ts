import { invoke, isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import type { ServiceHealth, ServiceMethod, ServiceParams, ServiceResult } from "./contracts";

type RequestArguments<M extends ServiceMethod> = ServiceParams<M> extends undefined
  ? []
  : [params: ServiceParams<M> & Record<string, unknown>];

export interface DesktopBridge {
  shellHealth(): Promise<{ available: boolean; version: string }>;
  request<M extends ServiceMethod>(method: M, ...args: RequestArguments<M>): Promise<ServiceResult<M>>;
  selectMediaInputs?(): Promise<string[] | null>;
  selectRelinkInput?(): Promise<string | null>;
  selectReferenceInput?(): Promise<string | null>;
  selectProjectParent?(): Promise<string | null>;
  selectProjectInput?(): Promise<string | null>;
}

export const desktopBridge: DesktopBridge = {
  async shellHealth() {
    if (!isTauri()) return { available: true, version: "browser-dev" };
    return invoke("shell_health");
  },
  async request<M extends ServiceMethod>(method: M, ...args: RequestArguments<M>): Promise<ServiceResult<M>> {
    const params = (args[0] ?? {}) as Record<string, unknown>;
    if (!isTauri()) {
      if (method === "ping") return { service: "localcut", version: "browser-dev", protocolVersion: 1 } as ServiceResult<M>;
      if (method === "project_open_last") throw new Error("No recent project is available in browser preview.");
      throw new Error("Start the Tauri shell to connect to the local service.");
    }
    return invoke<ServiceResult<M>>("service_request", { call: { method, params } });
  },
  async selectMediaInputs() {
    if (!isTauri()) return null;
    const selected = await open({ multiple: true, directory: true, filters: [{ name: "MP4 and MOV video", extensions: ["mp4", "mov"] }] });
    return selected === null ? null : Array.isArray(selected) ? selected : [selected];
  },
  async selectRelinkInput() {
    if (!isTauri()) return null;
    const selected = await open({ multiple: false, directory: false, title: "Choose the original or replacement MP4/MOV file", filters: [{ name: "MP4 and MOV video", extensions: ["mp4", "mov"] }] });
    return typeof selected === "string" ? selected : null;
  },
  async selectReferenceInput() {
    if (!isTauri()) return null;
    const selected = await open({ multiple: false, directory: false, title: "Choose a local MP4 or MOV reference", filters: [{ name: "MP4 and MOV video", extensions: ["mp4", "mov"] }] });
    return typeof selected === "string" ? selected : null;
  },
  async selectProjectParent() {
    if (!isTauri()) throw new Error("Start the Local Cut desktop app to create a native project. Browser preview cannot access the local service.");
    const selected = await open({ directory: true, multiple: false, title: "Choose where to save the new project" });
    return typeof selected === "string" ? selected : null;
  },
  async selectProjectInput() {
    if (!isTauri()) throw new Error("Start the Local Cut desktop app to open a native project. Browser preview cannot access the local service.");
    const selected = await open({ directory: true, multiple: false, title: "Open an existing .localcut project folder" });
    return typeof selected === "string" ? selected : null;
  },
};

export async function loadHealth(bridge: DesktopBridge = desktopBridge) {
  const shell = await bridge.shellHealth();
  try {
    const health = await bridge.request("health") as ServiceHealth;
    return { ui: { available: true, detail: "React interface is running." }, shell, ...health };
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unable to connect to local service.";
    return {
      ui: { available: true, detail: "React interface is running." },
      shell,
      service: { available: false, detail: message },
      ffmpeg: { available: false, detail: "Unavailable until the local service connects." },
      storage: { available: false, detail: "Unavailable until the local service connects." },
    } satisfies Record<string, unknown>;
  }
}
