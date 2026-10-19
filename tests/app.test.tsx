import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import App from "../src/ui/App";
import type { DesktopBridge } from "../src/bridge";

describe("desktop startup UI", () => {
  it("opens a selected native project and exposes its persistent editor", async () => {
    const state = { projectId: "chosen", revision: 3, settings: { projectName: "Chosen lesson" }, assets: [], timelineItems: [], captions: [], transcripts: [], analyses: [] };
    const request = vi.fn(async (method: string) => {
      if (method === "project_open_last") throw new Error("No recent project");
      if (method === "project_open") return { projectId: "chosen", path: "C:/Work/Chosen.localcut", state };
      if (method === "brief_list") return { briefs: [] };
      if (method === "revision_list") return { suggestions: [], revision: 3, warnings: [] };
      if (method === "proposal_list") return { plans: [] };
      if (method === "style_recipe_catalog") return { recipes: [] };
      if (method === "transcript_list") return { transcripts: [] };
      if (method === "models_status") return { models: [] };
      if (method === "timeline_get") return { timeline: { schemaVersion: 1, revision: 3, tracks: [], clips: [], captions: [], markers: [], durationTicks: 0, canUndo: false, canRedo: false } };
      if (method === "timeline_history") return { snapshots: [], truncated: false };
      return { jobs: [] };
    });
    render(<App bridge={{ shellHealth: vi.fn().mockResolvedValue({ available: true }), request, selectProjectInput: vi.fn().mockResolvedValue("C:/Work/Chosen.localcut") } as unknown as DesktopBridge} />);
    await screen.findByText("Create a project to import footage and start editing.");
    fireEvent.click(screen.getByRole("button", { name: "Open project" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_open", { path: "C:/Work/Chosen.localcut" }));
    expect(await screen.findByText("Chosen lesson")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Editor" }));
    expect(await screen.findByRole("region", { name: "Timeline editor" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save brief" })).toBeInTheDocument();
  });

  it("creates a named local project in a user-selected folder", async () => {
    const state = { projectId: "p2", revision: 0, settings: { projectName: "Lesson edit" }, assets: [], timelineItems: [], captions: [], transcripts: [], analyses: [] };
    const request = vi.fn().mockImplementation(async method => method === "project_open_last" ? (request.mock.calls.some(([name]) => name === "project_create") ? { projectId: "p2", path: "C:/Work/Lesson edit.localcut", state } : Promise.reject(new Error("none"))) : method === "project_create" ? { projectId: "p2" } : method === "health" ? ({ service: { available: true }, ffmpeg: { available: true }, storage: { available: true } }) : ({ jobs: [] }));
    const bridge = { shellHealth: vi.fn().mockResolvedValue({ available: true }), request, selectProjectParent: vi.fn().mockResolvedValue("C:/Work") } as unknown as DesktopBridge;
    render(<App bridge={bridge} />);
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), { target: { value: "Lesson edit" } });
    fireEvent.click(screen.getByRole("button", { name: "Choose folder and create" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_create", { path: "C:/Work/Lesson edit.localcut", name: "Lesson edit", settings: { projectName: "Lesson edit" } }));
    expect(await screen.findByText("Lesson edit")).toBeInTheDocument();
  });
  it("queues preview generation for the selected imported asset", async () => {
    const request = vi.fn().mockImplementation(async (method) => {
      if (method === "project_open_last") return { projectId: "p1", path: "C:/project", state: { revision: 1, settings: {}, assets: [{ id: "a1", original_uri: "lesson.mov" }] } };
      if (method === "proxy_generate") return { state: "queued", deduplicated: false };
      if (method === "job_list") return { jobs: [] };
      throw new Error("unavailable");
    });
    render(<App bridge={{ request, shellHealth: vi.fn().mockResolvedValue({ available: true }) }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Generate preview assets" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("proxy_generate", { path: "C:/project", assetId: "a1" }));
    expect(await screen.findByText(/Preview generation queued/)).toBeInTheDocument();
  });
  it("offers project-managed copies as an explicit import choice", async () => {
    const request = vi.fn().mockImplementation(async (method) => method === "project_open_last" ? { projectId: "p1", path: "C:/project", state: { revision: 1, settings: {}, assets: [] } } : method === "media_import" ? { imported: [{ status: "imported" }], errors: [] } : method === "health" ? { service: { available: true }, ffmpeg: { available: true }, storage: { available: true } } : { jobs: [] });
    const bridge = { request, shellHealth: vi.fn().mockResolvedValue({ available: true }), selectMediaInputs: vi.fn().mockResolvedValue(["C:/source/lesson.mov"]) } as unknown as DesktopBridge;
    render(<App bridge={bridge} />);
    fireEvent.click(await screen.findByRole("button", { name: "Copy into project" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("media_import", { path: "C:/project", inputs: ["C:/source/lesson.mov"], storageMode: "managed" }));
    expect(await screen.findByText(/copied into project/)).toBeInTheDocument();
  });
  it("requires explicit confirmation before relinking to changed media", async () => {
    let confirmRequired = true;
    const request = vi.fn().mockImplementation(async (method) => {
      if (method === "project_open_last") return { projectId: "p1", path: "C:/project", state: { revision: 1, settings: {}, assets: [{ id: "a1", original_uri: "C:/old/lesson.mov" }] } };
      if (method === "media_relink") { if (!confirmRequired) return { status: "replaced" }; confirmRequired = false; return { status: "confirmation_required" }; }
      if (method === "health") return { service: { available: true }, ffmpeg: { available: true }, storage: { available: true } };
      return { jobs: [] };
    });
    const bridge = { request, shellHealth: vi.fn().mockResolvedValue({ available: true }), selectRelinkInput: vi.fn().mockResolvedValue("C:/new/lesson.mov") } as unknown as DesktopBridge;
    render(<App bridge={bridge} />);
    fireEvent.click(await screen.findByRole("button", { name: "Relink source…" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not match the original fingerprint");
    fireEvent.click(screen.getByRole("button", { name: "Replace and invalidate generated artifacts" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("media_relink", { path: "C:/project", assetId: "a1", candidatePath: "C:/new/lesson.mov", allowChanged: true }));
    expect(await screen.findByText(/marked stale/)).toBeInTheDocument();
  });
  it("renders navigation and health with a live local service", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockImplementation(async (method) => method === "health" ? ({
        service: { available: true, detail: "Python service connected", version: "0.1.0" },
        ffmpeg: { available: false, detail: "FFmpeg not found" },
        storage: { available: true, detail: "Writable" },
      }) : method === "job_list" ? ({ jobs: [] }) : ({ projectId: "p1", path: "C:/lesson.localcut", state: { projectId: "p1", revision: 1, settings: { projectName: "Lesson" }, assets: [], timelineItems: [], transcripts: [], analyses: [] } })),
    };
    render(<App bridge={bridge} />);
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main navigation" }).querySelectorAll("button")).toHaveLength(8);
    await waitFor(() => expect(screen.getAllByText("Available")).toHaveLength(3));
    expect(screen.getByText("FFmpeg not found")).toBeInTheDocument();
    expect(screen.getByText("All systems ready")).toBeInTheDocument();
  });

  it("describes local processing without implying model downloads are offline", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockImplementation(async (method) => method === "health" ? ({
        service: { available: true }, ffmpeg: { available: true }, storage: { available: true },
      }) : method === "job_list" ? ({ jobs: [] }) : Promise.reject(new Error("no recent project"))),
    };
    render(<App bridge={bridge} />);

    await screen.findByText("Editing and inference stay local. Model downloads are explicit.");
    expect(screen.getByText("Editing and inference stay local. Model downloads are explicit.")).toBeInTheDocument();
    expect(screen.getByText("LOCAL PROCESSING")).toHaveAttribute("title", expect.stringContaining("Explicit model downloads contact their disclosed download host"));
    expect(screen.queryByText(/No uploads, ever/)).not.toBeInTheDocument();
  });

  it("opens and closes the keyboard shortcuts help dialog", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockImplementation(async (method) => method === "health"
        ? { service: { available: true }, ffmpeg: { available: true }, storage: { available: true } }
        : method === "job_list" ? { jobs: [] } : Promise.reject(new Error("no project"))),
    };
    render(<App bridge={bridge} />);
    const trigger = screen.getAllByRole("button", { name: "Help & shortcuts" })[0];
    fireEvent.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Help & shortcuts" })).toHaveTextContent("Ctrl");
    expect(screen.getByRole("dialog")).toHaveTextContent("Undo last timeline edit");
    fireEvent.keyDown(screen.getByRole("button", { name: "Close help" }), { key: "Tab" });
    expect(screen.getByRole("button", { name: "Close help" })).toHaveFocus();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("warns when the last project was restored from a recovery checkpoint", async () => {
    const request = vi.fn().mockImplementation(async (method) => method === "health"
      ? { service: { available: true }, ffmpeg: { available: true }, storage: { available: true } }
      : method === "project_open_last"
        ? { projectId: "p1", path: "C:/lesson.localcut", recovery: { checkpointRevision: 12, checkpointUpdatedAt: "2026-10-17T17:00:00+00:00", recoveredAt: "2026-10-17T18:00:00+00:00", message: "The project database was damaged. Local Cut restored the latest valid recovery checkpoint; changes after its timestamp may be missing." }, state: { projectId: "p1", revision: 12, settings: { projectName: "Lesson" }, assets: [], timelineItems: [], transcripts: [], analyses: [] } }
        : { jobs: [] });
    render(<App bridge={{ request, shellHealth: vi.fn().mockResolvedValue({ available: true }) }} />);
    const warning = await screen.findByRole("alert");
    expect(warning).toHaveTextContent("recovered from checkpoint · revision 12");
    expect(warning).toHaveTextContent("exact missing-work interval cannot be determined");
    expect(warning).toHaveTextContent("Review the recovered timeline and transcript");
  });

  it("shows missing source media in Projects and warns on affected timeline clips", async () => {
    const request = vi.fn().mockImplementation(async (method) => method === "health"
      ? { service: { available: true }, ffmpeg: { available: true }, storage: { available: true } }
      : method === "project_open_last"
        ? { projectId: "p1", path: "C:/lesson.localcut", state: { projectId: "p1", revision: 1, settings: {}, assets: [{ id: "a1", original_uri: "C:/missing/lesson.mp4", sourceStatus: "unavailable", duration_ticks: 10_000_000 }], timelineItems: [{ id: "clip1", assetId: "a1", itemType: "clip", trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 1_000_000, sourceInTicks: 0, sourceOutTicks: 1_000_000 }], transcripts: [], analyses: [] } }
        : method === "timeline_get" ? { timeline: { schemaVersion: 1, revision: 1, tracks: [{ id: "video", kind: "video", name: "Video" }], clips: [{ id: "clip1", assetId: "a1", itemType: "clip", trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 1_000_000, sourceInTicks: 0, sourceOutTicks: 1_000_000 }], captions: [], markers: [], durationTicks: 1_000_000, canUndo: false, canRedo: false } }
        : method === "brief_list" ? { briefs: [] }
          : method === "revision_list" ? { suggestions: [], revision: 1, warnings: [] }
          : method === "proposal_list" ? { plans: [] }
            : method === "transcript_list" ? { transcripts: [] }
              : method === "models_status" ? { models: [], recommended: "", backendAvailable: false }
              : { jobs: [] });
    render(<App bridge={{ request, shellHealth: vi.fn().mockResolvedValue({ available: true }) }} />);
    expect(await screen.findByText("Source missing or unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("navigation", { name: "Main navigation" }).querySelector("button:nth-child(2)")!);
    expect(await screen.findByRole("alert")).toHaveTextContent("timeline clip uses missing or changed source media");
    expect(screen.getByRole("alert")).toHaveTextContent("Visit Projects to relink");
  });

  it("navigates without requiring the service", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockRejectedValue(new Error("offline")),
    };
    render(<App bridge={bridge} />);
    fireEvent.click(screen.getByRole("navigation", { name: "Main navigation" }).querySelector("button:nth-child(2)")!);
    expect(screen.getByRole("heading", { name: "Transcript review" })).toBeInTheDocument();
  });

  it("provides a skip link and exposes the active navigation page", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockRejectedValue(new Error("offline")),
    };
    render(<App bridge={bridge} />);
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(skip).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByRole("main")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("button", { name: "Projects" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(screen.getByRole("button", { name: "Editor" }));
    expect(screen.getByRole("button", { name: "Editor" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Projects" })).not.toHaveAttribute("aria-current");
  });

  it("shows hardware and advertised media capabilities in Settings", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockImplementation(async (method) => method === "capabilities" ? ({
        schemaVersion: 1, collectedAt: "now", platform: { system: "Windows", release: "11", machine: "x64", pythonBits: "64bit" },
        cpu: { logicalCores: 8, brand: "Mock CPU", flags: ["avx2"], source: "mock", confidence: "reported" },
        memory: { totalBytes: 16 * 1024 ** 3, availableBytes: 8 * 1024 ** 3, source: "mock", confidence: "reported" },
        gpus: [], mediaTools: { ffmpeg: { available: true, version: "ffmpeg mock", encoders: ["h264_nvenc"], decoders: ["h264"], hwaccels: ["cuda"] }, ffprobe: { available: true, version: "ffprobe mock" }, encoderListingSemantics: "Advertised only" },
        disk: { totalBytes: 100, freeBytes: 50, source: "mock", confidence: "reported" }, recommendations: { modelSizeClass: "small", proxyPreset: "balanced", confidence: "estimated" },
      }) : Promise.reject(new Error("offline"))),
    };
    render(<App bridge={bridge} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Settings" })[0]!);
    expect(await screen.findByText("ffmpeg mock")).toBeInTheDocument();
    expect(screen.getByText(/Encoders: h264_nvenc/)).toBeInTheDocument();
    expect(screen.getByText(/Decoders: h264/)).toBeInTheDocument();
    expect(screen.getByText(/Hardware acceleration: cuda/)).toBeInTheDocument();
  });

  it("shows job stages and sends cancellation for a running job", async () => {
    const request = vi.fn().mockImplementation(async (method) => method === "health" ? ({ service: { available: true, detail: "ok" }, ffmpeg: { available: true }, storage: { available: true } }) : method === "project_open_last" ? Promise.reject(new Error("none")) : method === "job_list" ? ({ jobs: [{ id: "j1", projectId: "p1", kind: "proxy", state: "running", progress: 0.45, stage: "encoding", message: "Building proxy", attempts: 1, maxAttempts: 3, cancelRequested: false, dependencies: [], output: null, error: null, createdAt: "now" }] }) : ({ state: "running" }));
    const bridge: DesktopBridge = { shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }), request };
    render(<App bridge={bridge} />);
    fireEvent.click(screen.getByRole("button", { name: /^Jobs/ }));
    expect(await screen.findByText(/encoding · Building proxy/)).toBeInTheDocument();
    expect(screen.getByText("45% · attempt 1/3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel proxy job j1" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("job_cancel", { jobId: "j1" }));
  });
});
