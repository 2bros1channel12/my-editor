import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import WrittenPlanReview from "../src/ui/WrittenPlanReview";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse } from "../src/contracts";

const project = { projectId: "p", path: "C:/project", state: { assets: [{ id: "a", original_uri: "lesson.mov" }] } } as unknown as LastProjectResponse;
const selection = (id: string) => ({ candidateId: id, reason: "Supports the lesson", confidence: .7, confidenceReason: "Timing verified; editorial judgment", requirementIds: ["topic:0"] });
const section = (id: string, locked = false) => ({ id, title: id, purpose: "Explain the idea", status: "pending", locked, confidence: .7, estimatedDurationTicks: 500000, requirementsSatisfied: ["topic:0"], selections: [selection(id)], alternatives: [selection("alt")], alternativeNote: "Review the alternative" });
function record(locked = false) {
  return { id: "plan", briefId: "b", revision: 1, status: "pending", stale: false, plan: { assetId: "a", reviewVersion: 3, estimatedDurationTicks: 1000000, targetDurationTicks: null, provenance: { modelName: "local", modelVersion: "1" }, warnings: [], hook: { candidateId: "one", reason: "Open with evidence" }, requirements: [{ id: "topic:0", description: "Teach a useful lesson" }], unmetRequirements: [{ requirementId: "topic:1", reason: "No supported passage" }], candidates: ["one", "two", "alt"].map((id, index) => ({ id, assetId: "a", sourceInTicks: 2500000 + index * 1000000, sourceOutTicks: 3000000 + index * 1000000, excerpt: id === "one" ? "ignore instructions <script>render()</script>" : id, flags: [], uncertain: false, transcriptEdited: false })), sections: [section("one"), section("two", locked)] } };
}
function setup(initial = record()) {
  let saved = initial;
  const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === "brief_list") return { briefs: [{ id: "b", revision: 1, constraints: { objective: "Lesson" } }] };
    if (method === "planning_list") return { plans: [saved] };
    if (method === "job_list") return { jobs: [] };
    if (method === "timeline_get") return { timeline: { revision: 12 } };
    if (method === "timeline_compile") return { timeline: { revision: 13 } };
    if (method === "planning_generate") return { id: "job", state: "queued", deduplicated: false };
    if (method === "planning_review") { saved = { ...saved, plan: { ...saved.plan, reviewVersion: saved.plan.reviewVersion + 1 } }; return { plan: saved }; }
    if (method === "transcript_list") return { transcripts: [], playbackUrl: "http://127.0.0.1:1/proxy", sourceMinusProxySeconds: 2 };
    throw new Error(`Unexpected method ${method}: ${JSON.stringify(params)}`);
  });
  const bridge = { request } as unknown as DesktopBridge;
  return { request, bridge };
}

describe("written plan review", () => {
  it("queues a plan with selected evidence and renders source-backed writing safely", async () => {
    const { request, bridge } = setup();
    const view = render(<WrittenPlanReview bridge={bridge} project={project}/>);
    await screen.findByText("Plan revision 1 · pending");
    expect(screen.getByText("No supported passage", { exact: false })).toBeInTheDocument();
    expect(view.container.querySelector("script")).toBeNull();
    expect(screen.getAllByText("ignore instructions <script>render()</script>").length).toBeGreaterThan(0);
    expect(request.mock.calls.every(call => !String(call[0]).includes("timeline"))).toBe(true);
    fireEvent.change(screen.getByLabelText("Optional evidence search"), { target: { value: " explanation " } });
    fireEvent.change(screen.getByLabelText("Planning search mode"), { target: { value: "semantic" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate written plan" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("planning_generate", { path: project.path, briefId: "b", assetId: "a", query: "explanation", searchMode: "semantic" }));
    fireEvent.click(screen.getAllByRole("button", { name: "2.500–3.000s · a" })[0]);
    const video = await screen.findByLabelText("Written plan source preview") as HTMLVideoElement;
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(.5);
    expect(screen.getAllByText(/Source ticks: 2500000 to 3000000/).length).toBeGreaterThan(0);
  });

  it("sends explicit review decisions with optimistic versions and only validated replacements", async () => {
    const { request, bridge } = setup();
    render(<WrittenPlanReview bridge={bridge} project={project}/>);
    await screen.findByText("Plan revision 1 · pending");
    fireEvent.click(screen.getByRole("button", { name: "Approve one" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("planning_review", { path: project.path, planId: "plan", action: "approve", sectionId: "one", expectedReviewVersion: 3 }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Replace one" })).toBeDisabled());
    fireEvent.change(screen.getByLabelText("Replacement for one"), { target: { value: "alt" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Replace one" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Replace one" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("planning_review", { path: project.path, planId: "plan", action: "replace", sectionId: "one", candidateId: "alt", expectedReviewVersion: 4 }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Move one down" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Move one down" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("planning_review", { path: project.path, planId: "plan", action: "reorder", order: ["two", "one"], expectedReviewVersion: 5 }));
  });

  it("prevents moving locked sections indirectly and allows explicit unlocking", async () => {
    const { request, bridge } = setup(record(true));
    render(<WrittenPlanReview bridge={bridge} project={project}/>);
    await screen.findByText("Plan revision 1 · pending");
    expect(screen.getByRole("button", { name: "Move one down" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move two up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject two" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Unlock two" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("planning_review", { path: project.path, planId: "plan", action: "unlock", sectionId: "two", expectedReviewVersion: 3 }));
  });


  it("shows stale evidence and prevents saving decisions until regenerated", async () => {
    const stale = { ...record(), stale: true, staleReason: "Transcript changed" };
    const { request, bridge } = setup(stale);
    render(<WrittenPlanReview bridge={bridge} project={project}/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Transcript changed");
    expect(screen.getByRole("button", { name: "Approve one" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Lock one" })).toBeDisabled();
    expect(request.mock.calls.some(call => call[0] === "planning_review")).toBe(false);
    expect(screen.getByRole("button", { name: "Generate written plan" })).toBeEnabled();
  });

  it("compiles only explicitly approved sections with both review and project versions", async () => {
    const approved = record(); approved.plan.sections[0].status = "approved";
    const { request, bridge } = setup(approved);
    render(<WrittenPlanReview bridge={bridge} project={project}/>);
    await screen.findByText(/Plan revision 1.*pending/);
    fireEvent.click(screen.getByRole("button", { name: "Append approved sections to timeline" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("timeline_compile", { path: project.path, planId: "plan", sectionIds: ["one"], expectedReviewVersion: 3, expectedRevision: 12 }));
    expect(await screen.findByRole("status")).toHaveTextContent("Approved sections appended");
  });

  it("ignores outstanding old-project responses after switching projects", async () => {
    let resolveOld!: (value: unknown) => void;
    const old = new Promise(resolve => { resolveOld = resolve; });
    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === "job_list") return { jobs: [] };
      if (method === "brief_list") return { briefs: [] };
      if (method === "planning_list") return params?.path === project.path ? old : { plans: [] };
      return {};
    });
    const bridge = { request } as unknown as DesktopBridge;
    const view = render(<WrittenPlanReview bridge={bridge} project={project}/>);
    view.rerender(<WrittenPlanReview bridge={bridge} project={{ ...project, path: "C:/other", projectId: "other" }}/>);
    await waitFor(() => expect(request).toHaveBeenCalledWith("planning_list", { path: "C:/other" }));
    resolveOld({ plans: [record()] });
    await waitFor(() => expect(screen.queryByText("Plan revision 1 · pending")).not.toBeInTheDocument());
  });
});
