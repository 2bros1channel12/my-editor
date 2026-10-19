import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TimelineEditor from "../src/ui/TimelineEditor";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse } from "../src/contracts";

const project = { projectId: "p", path: "C:/project", state: { projectId: "p", revision: 1, settings: {}, timelineItems: [], captions: [], transcripts: [], analyses: [], assets: [{ id: "a", original_uri: "lesson.mov", duration_ticks: 10_000_000 }] } } as unknown as LastProjectResponse;

describe("nondestructive timeline", () => {
  it("restores prior timeline undo snapshots from the local autosave journal", async () => {
    const current = { id: "clip", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 9_000_000, sourceInTicks: 1_000_000, sourceOutTicks: 10_000_000, properties: {} };
    const prior = { ...current, timelineDurationTicks: 10_000_000, sourceInTicks: 0, sourceOutTicks: 10_000_000 };
    const saved: Array<Record<string, unknown>> = [];
    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === "timeline_history") return { snapshots: [{ items: [prior], captions: [] }], truncated: false };
      if (method === "project_save") { saved.push(params?.state as Record<string, unknown>); return { revision: 2 }; }
      if (method === "transcript_list") return { playbackUrl: "", sourceMinusProxySeconds: 0 };
      if (method === "export_list") return { exports: [] };
      return {};
    });
    const currentProject = { ...project, state: { ...project.state, timelineItems: [current] } } as LastProjectResponse;
    const refreshProject = vi.fn().mockResolvedValue(undefined);
    const view = render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={currentProject} onProjectRefresh={refreshProject} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Undo" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]).toMatchObject({ timelineItems: [expect.objectContaining({ sourceInTicks: 0, sourceOutTicks: 10_000_000 })] });
    await waitFor(() => expect(refreshProject).toHaveBeenCalledOnce());
    view.rerender(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={{ ...currentProject, state: { ...currentProject.state, revision: 2, timelineItems: [prior] } }} onProjectRefresh={refreshProject} />);
    expect(screen.getByRole("button", { name: "Redo" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Redo" }));
    await waitFor(() => expect(saved).toHaveLength(2));
    expect(saved[1]).toMatchObject({ timelineItems: [expect.objectContaining({ sourceInTicks: 1_000_000, sourceOutTicks: 10_000_000 })] });
  });

  it("autosaves source clips, split ranges, and editable captions", async () => {
    const request = vi.fn(async (method: string) => method === "transcript_list" ? { playbackUrl: "http://127.0.0.1:1/proxy", sourceMinusProxySeconds: 1.5, waveform: { sourceStartTicks: 0, durationTicks: 10_000_000, sampleCount: 160_000, originalBinCount: 2, peaks: [[-.5, .5], [-1, 1]] } } : method === "project_save" ? { revision: 2 } : {});
    const bridge = { request, shellHealth: vi.fn() } as unknown as DesktopBridge;
    render(<TimelineEditor bridge={bridge} project={project} />);
    expect(await screen.findByRole("img", { name: "Waveform from source audio" })).toBeInTheDocument();
    expect(screen.getByText(/2 source bins summarized/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Add source range" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ timelineItems: [expect.objectContaining({ itemType: "clip", sourceInTicks: 0, sourceOutTicks: 10_000_000 })] }) })));
    await screen.findByRole("button", { name: "lesson.mov" });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Clip source in" }), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Split at source in" }));
    await waitFor(() => expect(request).toHaveBeenLastCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ timelineItems: expect.arrayContaining([expect.objectContaining({ sourceInTicks: 0, sourceOutTicks: 2_000_000 }), expect.objectContaining({ sourceInTicks: 2_000_000, sourceOutTicks: 10_000_000 })]) }) })));
    fireEvent.change(screen.getByRole("textbox", { name: "New caption" }), { target: { value: "Intro text" } });
    fireEvent.click(screen.getByRole("button", { name: "Add caption" }));
    await waitFor(() => expect(request).toHaveBeenLastCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ captions: [expect.objectContaining({ text: "Intro text" })] }) })));
    const editor = await screen.findByRole("textbox", { name: "Edit caption text" });
    fireEvent.change(editor, { target: { value: "Edited intro" } });
    fireEvent.click(screen.getByRole("button", { name: "Save caption" }));
    await waitFor(() => expect(request).toHaveBeenLastCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ captions: [expect.objectContaining({ text: "Edited intro" })] }) })));
  });

  it("keeps linked captions inside a trimmed source range and removes captions outside it", async () => {
    const clip = { id: "clip-1", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 10_000_000, sourceInTicks: 0, sourceOutTicks: 10_000_000, properties: { linkedAudio: true } };
    const firstCue = { id: "cue-1", assetId: "a", itemType: "caption" as const, trackId: "captions", timelineStartTicks: 1_000_000, timelineDurationTicks: 3_000_000, sourceInTicks: 1_000_000, sourceOutTicks: 4_000_000, properties: { text: "Keep this part", linkedClipId: "clip-1" } };
    const outsideCue = { id: "cue-2", assetId: "a", itemType: "caption" as const, trackId: "captions", timelineStartTicks: 5_000_000, timelineDurationTicks: 2_000_000, sourceInTicks: 5_000_000, sourceOutTicks: 7_000_000, properties: { text: "Trimmed away", linkedClipId: "clip-1" } };
    const followingClip = { id: "clip-2", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 10_000_000, timelineDurationTicks: 5_000_000, sourceInTicks: 0, sourceOutTicks: 5_000_000, properties: { linkedAudio: true } };
    const followingCue = { id: "cue-3", assetId: "a", itemType: "caption" as const, trackId: "captions", timelineStartTicks: 11_000_000, timelineDurationTicks: 1_000_000, sourceInTicks: 1_000_000, sourceOutTicks: 2_000_000, properties: { text: "Following clip", linkedClipId: "clip-2" } };
    const trimmedProject = { ...project, state: { ...project.state, timelineItems: [clip, firstCue, outsideCue, followingClip, followingCue], captions: [{ id: "text-1", timelineItemId: "cue-1", text: "Keep this part", language: "en", style: {} }, { id: "text-2", timelineItemId: "cue-2", text: "Trimmed away", language: "en", style: {} }, { id: "text-3", timelineItemId: "cue-3", text: "Following clip", language: "en", style: {} }] } } as LastProjectResponse;
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => method === "project_save" ? { revision: 2 } : method === "transcript_list" ? { playbackUrl: "", sourceMinusProxySeconds: 0 } : { exports: [] });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={trimmedProject} />);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Clip source in" }), { target: { value: "2" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Clip source out" }), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply trim" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.any(Object)));
    const save = request.mock.calls.find(([method]) => method === "project_save");
    const state = save?.[1]?.state as { timelineItems: Array<Record<string, unknown>>; captions: Array<Record<string, unknown>> };
    expect(state.timelineItems.find(item => item.id === "clip-1")).toMatchObject({ sourceInTicks: 2_000_000, sourceOutTicks: 3_000_000, timelineDurationTicks: 1_000_000 });
    expect(state.timelineItems.find(item => item.id === "cue-1")).toMatchObject({ sourceInTicks: 2_000_000, sourceOutTicks: 3_000_000, timelineStartTicks: 0, timelineDurationTicks: 1_000_000 });
    expect(state.timelineItems.some(item => item.id === "cue-2")).toBe(false);
    expect(state.timelineItems.find(item => item.id === "clip-2")).toMatchObject({ timelineStartTicks: 1_000_000 });
    expect(state.timelineItems.find(item => item.id === "cue-3")).toMatchObject({ timelineStartTicks: 2_000_000 });
    expect(state.captions.map(item => item.timelineItemId)).toEqual(["cue-1", "cue-3"]);
  });

  it("splits linked captions by source overlap and keeps their caption records valid", async () => {
    const clip = { id: "clip-1", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 10_000_000, sourceInTicks: 0, sourceOutTicks: 10_000_000, properties: { linkedAudio: true } };
    const cue = (id: string, start: number, end: number, text: string) => ({ id, assetId: "a", itemType: "caption" as const, trackId: "captions", timelineStartTicks: start, timelineDurationTicks: end - start, sourceInTicks: start, sourceOutTicks: end, properties: { text, linkedClipId: "clip-1" } });
    const splitProject = { ...project, state: { ...project.state, timelineItems: [clip, cue("before", 1_000_000, 3_000_000, "Before"), cue("after", 7_000_000, 9_000_000, "After"), cue("crossing", 4_000_000, 6_000_000, "Crossing")], captions: ["before", "after", "crossing"].map(id => ({ id: `record-${id}`, timelineItemId: id, text: id, language: "en", style: {} })) } } as LastProjectResponse;
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => method === "project_save" ? { revision: 2 } : method === "transcript_list" ? { playbackUrl: "", sourceMinusProxySeconds: 0 } : { exports: [] });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={splitProject} />);
    fireEvent.click(await screen.findByRole("button", { name: "lesson.mov" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Clip source in" }), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Split at source in" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.any(Object)));
    const save = request.mock.calls.find(([method]) => method === "project_save");
    const state = save?.[1]?.state as { timelineItems: Array<Record<string, unknown>>; captions: Array<Record<string, unknown>> };
    const children = state.timelineItems.filter(item => item.itemType === "caption");
    const rightClip = state.timelineItems.find(item => item.itemType === "clip" && item.id !== "clip-1")!;
    expect(children.find(item => item.id === "before")).toMatchObject({ sourceInTicks: 1_000_000, sourceOutTicks: 3_000_000, properties: { linkedClipId: "clip-1" } });
    expect(children.find(item => item.id === "after")).toMatchObject({ sourceInTicks: 7_000_000, sourceOutTicks: 9_000_000, properties: { linkedClipId: rightClip.id } });
    expect(children.find(item => item.id === "crossing")).toMatchObject({ sourceInTicks: 4_000_000, sourceOutTicks: 5_000_000, properties: { linkedClipId: "clip-1" } });
    expect(children.find(item => item.id !== "crossing" && (item.properties as Record<string, unknown>).text === "Crossing")).toMatchObject({ sourceInTicks: 5_000_000, sourceOutTicks: 6_000_000, properties: { linkedClipId: rightClip.id } });
    expect(state.captions.map(item => item.timelineItemId).sort()).toEqual(children.map(item => item.id).sort());
  });

  it("moves linked caption timing with the source range when clips reorder", async () => {
    const first = { id: "first", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 10_000_000, sourceInTicks: 0, sourceOutTicks: 10_000_000, properties: {} };
    const second = { ...first, id: "second", timelineStartTicks: 10_000_000 };
    const cue = { id: "cue", assetId: "a", itemType: "caption" as const, trackId: "captions", timelineStartTicks: 2_000_000, timelineDurationTicks: 2_000_000, sourceInTicks: 2_000_000, sourceOutTicks: 4_000_000, properties: { text: "Keep timing", linkedClipId: "first" } };
    const reorderProject = { ...project, state: { ...project.state, timelineItems: [first, second, cue], captions: [{ id: "cue-record", timelineItemId: "cue", text: "Keep timing", language: "en", style: {} }] } } as LastProjectResponse;
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => method === "project_save" ? { revision: 2 } : method === "transcript_list" ? { playbackUrl: "", sourceMinusProxySeconds: 0 } : { exports: [] });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={reorderProject} />);
    fireEvent.click((await screen.findAllByRole("button", { name: "lesson.mov" }))[0]);
    fireEvent.click(screen.getByRole("button", { name: "Move later" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.any(Object)));
    const state = request.mock.calls.find(([method]) => method === "project_save")?.[1]?.state as { timelineItems: Array<Record<string, unknown>> };
    expect(state.timelineItems.find(item => item.id === "second")?.timelineStartTicks).toBe(0);
    expect(state.timelineItems.find(item => item.id === "first")?.timelineStartTicks).toBe(10_000_000);
    expect(state.timelineItems.find(item => item.id === "cue")).toMatchObject({ sourceInTicks: 2_000_000, sourceOutTicks: 4_000_000, timelineStartTicks: 12_000_000, timelineDurationTicks: 2_000_000 });
  });

  it("ripple deletion shifts linked captions by their own source offset", async () => {
    const first = { id: "first", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 10_000_000, sourceInTicks: 0, sourceOutTicks: 10_000_000, properties: {} };
    const second = { ...first, id: "second", timelineStartTicks: 10_000_000 };
    const cue = { id: "cue", assetId: "a", itemType: "caption" as const, trackId: "captions", timelineStartTicks: 13_000_000, timelineDurationTicks: 1_000_000, sourceInTicks: 3_000_000, sourceOutTicks: 4_000_000, properties: { text: "Retain offset", linkedClipId: "second" } };
    const rippleProject = { ...project, state: { ...project.state, timelineItems: [first, second, cue], captions: [{ id: "cue-record", timelineItemId: "cue", text: "Retain offset", language: "en", style: {} }] } } as LastProjectResponse;
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => method === "project_save" ? { revision: 2 } : method === "transcript_list" ? { playbackUrl: "", sourceMinusProxySeconds: 0 } : { exports: [] });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={rippleProject} />);
    fireEvent.click((await screen.findAllByRole("button", { name: "lesson.mov" }))[0]);
    fireEvent.click(screen.getByRole("button", { name: "Ripple delete" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.any(Object)));
    const state = request.mock.calls.find(([method]) => method === "project_save")?.[1]?.state as { timelineItems: Array<Record<string, unknown>> };
    expect(state.timelineItems.find(item => item.id === "second")?.timelineStartTicks).toBe(0);
    expect(state.timelineItems.find(item => item.id === "cue")).toMatchObject({ sourceInTicks: 3_000_000, sourceOutTicks: 4_000_000, timelineStartTicks: 3_000_000, timelineDurationTicks: 1_000_000 });
  });

  it("serializes rapid timeline saves so the latest snapshot is committed last", async () => {
    let finishFirst!: (value: { revision: number }) => void;
    const request = vi.fn((method: string, _params?: Record<string, unknown>) => {
      if (method === "project_save") {
        if (request.mock.calls.filter(([name]) => name === "project_save").length === 1) return new Promise<{ revision: number }>(resolve => { finishFirst = resolve; });
        return Promise.resolve({ revision: 3 });
      }
      if (method === "transcript_list") return Promise.resolve({ playbackUrl: "", sourceMinusProxySeconds: 0 });
      if (method === "export_list") return Promise.resolve({ exports: [] });
      return Promise.resolve({});
    });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={project} />);
    fireEvent.click(await screen.findByRole("button", { name: "Add source range" }));
    await waitFor(() => expect(request.mock.calls.filter(([method]) => method === "project_save")).toHaveLength(1));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Clip source out" }), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply trim" }));
    expect(request.mock.calls.filter(([method]) => method === "project_save")).toHaveLength(1);
    finishFirst({ revision: 2 });
    await waitFor(() => expect(request.mock.calls.filter(([method]) => method === "project_save")).toHaveLength(2));
    const saves = request.mock.calls.filter(([method]) => method === "project_save");
    expect(saves[1][1]?.state).toMatchObject({ timelineItems: [expect.objectContaining({ sourceOutTicks: 8_000_000, timelineDurationTicks: 8_000_000 })] });
  });

  it("keeps a failed local edit available and offers a save retry", async () => {
    let saves = 0;
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === "project_save" && saves++ === 0) throw new Error("Disk is full.");
      if (method === "project_save") return { revision: 2 };
      if (method === "transcript_list") return { playbackUrl: "", sourceMinusProxySeconds: 0 };
      if (method === "export_list") return { exports: [] };
      return {};
    });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={project} />);
    fireEvent.click(await screen.findByRole("button", { name: "Add source range" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry save" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved locally · revision 2"));
    const savedState = request.mock.calls.find(([method]) => method === "project_save")?.[1]?.state as { timelineItems: unknown[] };
    expect(savedState.timelineItems).toHaveLength(1);
  });

  it("adds a linked user-supplied audio bed with source and timeline ranges", async () => {
    const clip = { id: "video-clip", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 4_000_000, sourceInTicks: 0, sourceOutTicks: 4_000_000, properties: { linkedAudio: true } };
    const audioProject = { ...project, state: { ...project.state, timelineItems: [clip], assets: [...project.state.assets, { id: "music", original_uri: "licensed-bed.mov", last_known_path: "licensed-bed.mov", duration_ticks: 3_000_000, metadata_json: JSON.stringify({ audio: [{ codec: "aac" }] }) }] } } as LastProjectResponse;
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => method === "project_save" ? { revision: 2 } : method === "transcript_list" ? { playbackUrl: "", sourceMinusProxySeconds: 0 } : { exports: [] });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={audioProject} />);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Audio bed source in" }), { target: { value: "0.5" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Audio bed timeline start" }), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add audio bed" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.any(Object)));
    const save = request.mock.calls.find(([method]) => method === "project_save");
    const state = save?.[1]?.state as { timelineItems: Array<Record<string, unknown>> };
    expect(state.timelineItems.find(item => item.itemType === "audio")).toMatchObject({ assetId: "music", trackId: "music", sourceInTicks: 500_000, sourceOutTicks: 3_000_000, timelineStartTicks: 1_000_000, timelineDurationTicks: 2_500_000, properties: { role: "music-bed", userSupplied: true, audioGainDb: -18 } });
    expect(await screen.findByText(/licensed-bed.mov · -18 dB/)).toBeInTheDocument();
  });

  it("saves nondestructive visual fade settings on the selected clip", async () => {
    const clip = { id: "clip-1", assetId: "a", itemType: "clip" as const, trackId: "video", timelineStartTicks: 0, timelineDurationTicks: 10_000_000, sourceInTicks: 0, sourceOutTicks: 10_000_000, properties: {} };
    const clipProject = { ...project, state: { ...project.state, timelineItems: [clip] } } as LastProjectResponse;
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => method === "project_save" ? { revision: 2 } : method === "transcript_list" ? { playbackUrl: "", sourceMinusProxySeconds: 0 } : { exports: [] });
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={clipProject} />);
    fireEvent.change(screen.getByRole("spinbutton", { name: "Video fade in seconds" }), { target: { value: "1" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Video fade out seconds" }), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply video fades" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.any(Object)));
    const save = request.mock.calls.find(([method]) => method === "project_save");
    const state = save?.[1]?.state as { timelineItems: Array<Record<string, unknown>> };
    expect(state.timelineItems[0]).toMatchObject({ properties: { videoFadeInTicks: 1_000_000, videoFadeOutTicks: 2_000_000 } });
  });

  it("supports Ctrl+Z and Ctrl+Shift+Z while preserving typing shortcuts in fields", async () => {
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => method === "project_save" ? { revision: 2 } : method === "transcript_list" ? { playbackUrl: "", sourceMinusProxySeconds: 0 } : {});
    render(<TimelineEditor bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={project} />);
    const sourceIn = screen.getByRole("spinbutton", { name: "Clip source in" });
    fireEvent.change(sourceIn, { target: { value: "1" } });
    fireEvent.keyDown(sourceIn, { key: "z", ctrlKey: true });
    expect(request).not.toHaveBeenCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ timelineItems: [] }) }));

    fireEvent.click(screen.getByRole("button", { name: "Add source range" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ timelineItems: [expect.any(Object)] }) })));
    fireEvent.keyDown(window, { key: "z", ctrlKey: true });
    await waitFor(() => expect(request).toHaveBeenLastCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ timelineItems: [] }) })));
    fireEvent.keyDown(window, { key: "z", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(request).toHaveBeenLastCalledWith("project_save", expect.objectContaining({ state: expect.objectContaining({ timelineItems: [expect.any(Object)] }) })));
  });
});
