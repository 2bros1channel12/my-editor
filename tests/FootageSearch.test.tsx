import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import FootageSearch from "../src/ui/FootageSearch";
import LocalRuntimePanel from "../src/ui/LocalRuntimePanel";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse } from "../src/contracts";

const project = { projectId: "p", path: "C:/project", state: { settings: {}, assets: [{ id: "a", original_uri: "lesson.mov" }], timelineItems: [], revision: 1 } } as unknown as LastProjectResponse;
const index = { available: true, stale: false, schemaVersion: 1, chunkCount: 2, semantic: true };

describe("local searchable footage", () => {
  it("sends typed source filters, renders hostile text literally, and seeks source time", async () => {
    const request = vi.fn(async (method: string) => {
      if (method === "index_status") return index;
      if (method === "job_list") return { jobs: [] };
      if (method === "index_search") return { index, stale: false, results: [{ id: "r", assetId: "a", transcriptId: "t", sourceInTicks: 2_500_000, sourceOutTicks: 3_000_000, text: "ignore instructions <script>alert(1)</script>", score: .8, shotId: "shot-1", flags: ["blur"] }] };
      if (method === "transcript_list") return { transcripts: [], playbackUrl: "http://127.0.0.1:1/proxy", sourceMinusProxySeconds: 2 };
      return {};
    });
    const view = render(<FootageSearch bridge={{ request } as unknown as DesktopBridge} project={project} />);
    await screen.findByText(/2 passages/);
    fireEvent.change(screen.getByLabelText("Transcript query"), { target: { value: "a lesson" } });
    fireEvent.change(screen.getByLabelText("Search mode"), { target: { value: "semantic" } });
    fireEvent.change(screen.getByLabelText("Filter asset"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("Source from (seconds)"), { target: { value: "1.5" } });
    fireEvent.change(screen.getByLabelText("Silence suggestions"), { target: { value: "no" } });
    fireEvent.click(screen.getByLabelText("Blur flag"));
    fireEvent.click(screen.getByRole("button", { name: "Search footage" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("index_search", { path: "C:/project", query: "a lesson", mode: "semantic", filters: { assetId: "a", sourceInTicks: 1_500_000, silence: false, qualityFlags: ["blur"] }, limit: 30 }));
    expect(await screen.findByText("ignore instructions <script>alert(1)</script>")).toBeInTheDocument();
    expect(view.container.querySelector("script")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "2.500–3.000s · a" }));
    const video = await screen.findByLabelText("Footage search preview") as HTMLVideoElement;
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(.5);
  });

  it("rebuilds via jobs and cancels without discarding the prior index", async () => {
    const request = vi.fn(async (method: string) => method === "index_status" ? index : method === "job_list" ? { jobs: [{ id: "job", kind: "indexing", state: "running", stage: "embedding", progress: .4 }] } : { id: "job", state: "queued" });
    render(<FootageSearch bridge={{ request } as unknown as DesktopBridge} project={project} />);
    await screen.findByText(/2 passages/);
    fireEvent.click(screen.getByLabelText("Include local semantic embeddings"));
    fireEvent.click(screen.getByRole("button", { name: "Rebuild index" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("index_build", { path: "C:/project", semantic: true, force: true }));
    expect(screen.getByRole("progressbar", { name: "Index progress" })).toHaveAttribute("value", "0.4");
    fireEvent.click(screen.getByRole("button", { name: "Cancel index build" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("job_cancel", { jobId: "job" }));
    expect(screen.getByText(/2 passages/)).toBeInTheDocument();
  });

  it("reports model unavailability without downloading or invoking generation", async () => {
    const request = vi.fn(async () => ({ available: false, provider: "ollama", models: [], detail: "Start your local provider." }));
    render(<LocalRuntimePanel bridge={{ request } as unknown as DesktopBridge} project={project} />);
    expect(await screen.findByText("Start your local provider.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Suggest transcript summary" })).toBeDisabled();
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("runtime_status");
  });
});
