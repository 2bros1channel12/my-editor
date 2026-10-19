import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AnalysisPage from "../src/ui/AnalysisPage";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse } from "../src/contracts";

const project = { projectId: "p", path: "C:/project", state: { projectId: "p", revision: 1, settings: {}, timelineItems: [], transcripts: [], analyses: [], assets: [{ id: "a", original_uri: "lesson.mov" }] } } as LastProjectResponse;

describe("footage analysis review", () => {
  it("runs one configured detector and seeks using the source-to-proxy offset", async () => {
    const request = vi.fn(async (method: string) => {
      if (method === "analysis_catalog") return { detectors: [{ id: "silence", name: "Silence candidates", version: "4", settings: { thresholdDbfs: [-38, -80, -12], hysteresisDb: [4, 0, 12] }, needsAudio: true }] };
      if (method === "analysis_list") return { results: [{ id: "r", assetId: "a", detectorId: "silence", detectorVersion: "4", sourceInTicks: 2_500_000, sourceOutTicks: 3_000_000, confidence: null, detectorName: "localcut-silence", artifactId: "artifact", sourceFingerprint: "f".repeat(64), config: { thresholdDbfs: -38, hysteresisDb: 4 }, createdAt: "2026-10-17T10:00:00Z", validity: "integrity_ok", result: { detectorName: "Silence candidates", detectorVersion: "4", suggestionOnly: true, explanation: "Quiet passage candidate.", signalStrength: 0.7, meanQuietWindowRmsDbfs: -54, thresholdMarginDb: 16 } }], sourceStatus: "metadata_matches_import", prerequisites: { proxyReady: true, hasAudio: true, transcriptReady: true } };
      if (method === "job_list") return { jobs: [] };
      if (method === "transcript_list") return { playbackUrl: "http://127.0.0.1:1/proxy", sourceMinusProxySeconds: 2 };
      return { state: "queued" };
    });
    const bridge = { request, shellHealth: vi.fn() } as unknown as DesktopBridge;
    render(<AnalysisPage bridge={bridge} project={project} />);
    await screen.findByText("Quiet passage candidate.");
    expect(screen.getByText(/Hysteresis keeps it active through the higher exit level/)).toBeInTheDocument();
    expect(screen.getByText(/Mean quiet-window level -54.0 dBFS RMS · 16.0 dB below threshold · measured signal, not confidence/)).toBeInTheDocument();
    expect(screen.getByText(/measurement only; confidence not provided/)).toBeInTheDocument();
    expect(screen.getByLabelText("hysteresisDb")).toHaveValue(4);
    expect(screen.getByText("Hysteresis band (dB)")).toBeInTheDocument();
    expect(screen.getByText(/Source status checks file size and modification time/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Run settings and provenance"));
    expect(screen.getByText(/thresholdDbfs: -38 · hysteresisDb: 4/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "2.50–3.00s" }));
    const video = screen.getByLabelText("Source preview") as HTMLVideoElement;
    expect(video.currentTime).toBe(0.5);
    fireEvent.change(screen.getByLabelText("thresholdDbfs"), { target: { value: "-30" } });
    fireEvent.change(screen.getByLabelText("hysteresisDb"), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Run this detector" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("analysis_run", { path: "C:/project", assetId: "a", detector: "silence", settings: { thresholdDbfs: -30, hysteresisDb: 6 } }));
    expect(screen.getByText(/nothing is removed or changed automatically/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("analysis_list", { path: "C:/project", assetId: "a", verifyIntegrity: true }));
  });

  it("retains stale analysis history but disables source seeks until rerun", async () => {
    const request = vi.fn(async (method: string) => method === "analysis_catalog"
      ? { detectors: [{ id: "silence", name: "Silence candidates", version: "1", settings: {}, needsAudio: true }] }
      : method === "analysis_list" ? { results: [{ id: "old", assetId: "a", detectorId: "silence", detectorVersion: "1", sourceInTicks: 2_500_000, sourceOutTicks: 3_000_000, confidence: null, detectorName: "localcut-silence", artifactId: "old-artifact", sourceFingerprint: "f".repeat(64), config: {}, createdAt: "2026-10-17T10:00:00Z", validity: "stale", validityReason: "The proxy changed.", result: { detectorName: "Silence candidates", detectorVersion: "1", suggestionOnly: true, explanation: "Old quiet-passage candidate.", signalStrength: 0.7 } }], sourceStatus: "metadata_matches_import", prerequisites: { proxyReady: true, hasAudio: true, transcriptReady: true } }
        : method === "job_list" ? { jobs: [] }
          : method === "transcript_list" ? { playbackUrl: "http://127.0.0.1:1/proxy", sourceMinusProxySeconds: 0 }
            : {});
    render(<AnalysisPage bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={project} />);

    expect(await screen.findByText("Old quiet-passage candidate.")).toBeInTheDocument();
    expect(screen.getByText(/Measured silence level unavailable for this saved result · rerun this detector/)).toBeInTheDocument();
    expect(screen.queryByText(/Detector signal 70%/)).not.toBeInTheDocument();
    expect(screen.getByText(/Seeking is disabled/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "2.50–3.00s" })).toBeDisabled();
    expect(screen.getByText("The proxy changed.")).toBeInTheDocument();
  });

  it("explains when a saved result came from an older detector version", async () => {
    const reason = "This result was created by detector version 3; current version is 4. Rerun the detector to review current results.";
    const request = vi.fn(async (method: string) => method === "analysis_catalog"
      ? { detectors: [{ id: "silence", name: "Silence candidates", version: "4", settings: {}, needsAudio: true }] }
      : method === "analysis_list" ? { results: [{ id: "old-version", assetId: "a", detectorId: "silence", detectorVersion: "3", sourceInTicks: 1_000_000, sourceOutTicks: 2_000_000, confidence: null, detectorName: "localcut-silence", artifactId: "old-version-artifact", sourceFingerprint: "f".repeat(64), config: {}, createdAt: "2026-10-17T10:00:00Z", validity: "stale", validityReason: reason, result: { detectorName: "Silence candidates", detectorVersion: "3", suggestionOnly: true, explanation: "Historical suggestion.", signalStrength: .5 } }], sourceStatus: "metadata_matches_import", prerequisites: { proxyReady: true, hasAudio: true, transcriptReady: true } }
        : method === "job_list" ? { jobs: [] }
          : method === "transcript_list" ? { playbackUrl: undefined, sourceMinusProxySeconds: 0 }
            : {});
    render(<AnalysisPage bridge={{ request, shellHealth: vi.fn() } as unknown as DesktopBridge} project={project} />);

    expect(await screen.findByText("Historical suggestion.")).toBeInTheDocument();
    expect(screen.getByText("Detector version changed")).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1.00–2.00s" })).toBeDisabled();
  });

  it("filters cached detector results by run while retaining history",async()=>{
    const result=(id:string,artifactId:string,createdAt:string,explanation:string)=>({id,assetId:"a",detectorId:"silence",detectorVersion:"1",sourceInTicks:0,sourceOutTicks:1_000_000,confidence:null,detectorName:"localcut-silence",artifactId,sourceFingerprint:"f".repeat(64),config:{thresholdDbfs:-38},createdAt,result:{detectorName:"Silence candidates",detectorVersion:"1",explanation,signalStrength:.5,suggestionOnly:true as const}});
    const request=vi.fn(async(method:string)=>method==="analysis_catalog"?{detectors:[{id:"silence",name:"Silence candidates",version:"1",settings:{thresholdDbfs:[-38,-80,-12]},needsAudio:true}]}:method==="analysis_list"?{results:[result("old","artifact-old","2026-10-16T10:00:00Z","Older run result."),result("new","artifact-new","2026-10-17T10:00:00Z","Latest run result.")],sourceStatus:"changed_since_import"}:method==="job_list"?{jobs:[]}:method==="transcript_list"?{playbackUrl:"http://127.0.0.1:1/proxy",sourceMinusProxySeconds:0}:{});
    const bridge={request,shellHealth:vi.fn()} as unknown as DesktopBridge;
    render(<AnalysisPage bridge={bridge} project={project}/>);
    expect(await screen.findByText("Latest run result.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Source changed since import");
    expect(screen.queryByText("Older run result.")).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox",{name:"Result run"}),{target:{value:"all"}});
    expect(await screen.findByText("Older run result.")).toBeInTheDocument();
  });
  it("explains filler matching and blocks missing transcript prerequisites before queueing",async()=>{
    const request=vi.fn(async(method:string)=>method==="analysis_catalog"?{detectors:[{id:"filler_words",name:"Possible filler words",version:"1",settings:{mergeGapMs:[250,0,1500]},needsTranscript:true}]}:method==="analysis_list"?{results:[],sourceStatus:"metadata_matches_import",prerequisites:{proxyReady:true,hasAudio:true,transcriptReady:false}}:method==="job_list"?{jobs:[]}:method==="transcript_list"?{playbackUrl:undefined,sourceMinusProxySeconds:0}:{});
    const onNavigate=vi.fn();
    const bridge={request,shellHealth:vi.fn()} as unknown as DesktopBridge;
    const multiAssetProject={...project,state:{...project.state,assets:[...project.state.assets,{id:"b",original_uri:"second-lesson.mov"}]}} as LastProjectResponse;
    render(<AnalysisPage bridge={bridge} project={multiAssetProject} onNavigate={onNavigate}/>);
    expect(await screen.findByText(/Exact um, uh, erm, and er token matches/)).toBeInTheDocument();
    expect(await screen.findByText("Create a current transcript in Editor before running this detector.")).toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Run this detector"})).toBeDisabled();
    expect(request).not.toHaveBeenCalledWith("analysis_run",expect.anything());
    fireEvent.change(screen.getByRole("combobox",{name:"Analysis source footage"}),{target:{value:"b"}});
    await waitFor(()=>expect(request).toHaveBeenCalledWith("analysis_list",{path:"C:/project",assetId:"b"}));
    fireEvent.click(screen.getByRole("button",{name:"Open Editor to create a transcript"}));
    expect(onNavigate).toHaveBeenCalledWith("Editor","b");
  });
  it("routes missing proxy prerequisites to Projects",async()=>{
    const request=vi.fn(async(method:string)=>method==="analysis_catalog"?{detectors:[{id:"silence",name:"Silence candidates",version:"1",settings:{thresholdDbfs:[-38,-80,-12]},needsAudio:true}]}:method==="analysis_list"?{results:[],sourceStatus:"metadata_matches_import",prerequisites:{proxyReady:false,hasAudio:false,transcriptReady:false}}:method==="job_list"?{jobs:[]}:method==="transcript_list"?{playbackUrl:undefined,sourceMinusProxySeconds:0}:{});
    const onNavigate=vi.fn();
    render(<AnalysisPage bridge={{request,shellHealth:vi.fn()} as unknown as DesktopBridge} project={project} onNavigate={onNavigate}/>);
    expect(await screen.findByText("Generate preview assets in Projects before running a detector.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Open Projects to generate preview assets"}));
    expect(onNavigate).toHaveBeenCalledWith("Projects", undefined);
  });
  it("labels filler word confidence as ASR token confidence, not detector confidence",async()=>{
    const request=vi.fn(async(method:string)=>method==="analysis_catalog"?{detectors:[{id:"filler_words",name:"Possible filler words",version:"1",settings:{mergeGapMs:[250,0,1500]},needsTranscript:true}]}:method==="analysis_list"?{results:[{id:"f",assetId:"a",detectorId:"filler_words",detectorVersion:"1",sourceInTicks:1_000_000,sourceOutTicks:1_200_000,confidence:.82,detectorName:"localcut-filler_words",artifactId:"artifact",sourceFingerprint:"f".repeat(64),config:{},createdAt:"2026-10-17T10:00:00Z",result:{detectorName:"Possible filler words",detectorVersion:"1",explanation:"Matched an ASR token.",signalStrength:1,suggestionOnly:true,terms:["um"]}}],sourceStatus:"metadata_matches_import",prerequisites:{proxyReady:true,hasAudio:true,transcriptReady:true}}:method==="job_list"?{jobs:[]}:method==="transcript_list"?{playbackUrl:undefined,sourceMinusProxySeconds:0}:{});
    const bridge={request,shellHealth:vi.fn()} as unknown as DesktopBridge;
    render(<AnalysisPage bridge={bridge} project={project}/>);
    expect(await screen.findByText(/ASR token confidence 82%, not detector confidence/)).toBeInTheDocument();
    expect(screen.queryByText(/· confidence 82%$/)).not.toBeInTheDocument();
  });
  it("discloses incomplete repeated-take searches and treats thumbnails as point samples",async()=>{
    const request=vi.fn(async(method:string)=>method==="analysis_catalog"?{detectors:[{id:"thumbnails",name:"Representative thumbnails",version:"1",settings:{count:[5,1,12]},needsVideo:true},{id:"repeated_takes",name:"Possible repeated takes",version:"3",settings:{similarityThreshold:[.78,.5,1]},needsTranscript:true}]}:method==="analysis_list"?{results:[{id:"thumb",assetId:"a",detectorId:"thumbnails",detectorVersion:"1",sourceInTicks:1_250_000,sourceOutTicks:1_250_001,confidence:null,detectorName:"localcut-thumbnails",artifactId:"thumb-artifact",sourceFingerprint:"f".repeat(64),config:{},createdAt:"2026-10-17T10:00:00Z",result:{detectorName:"Representative thumbnails",detectorVersion:"1",explanation:"Exact sampled frame.",signalStrength:1,suggestionOnly:true,sampleIsPoint:true}}],warnings:[{detectorId:"repeated_takes",artifactId:"repeat-artifact",code:"repeated_take_comparison_limit",message:"This repeated-take result set may be incomplete."}],sourceStatus:"metadata_matches_import",prerequisites:{proxyReady:true,hasAudio:true,transcriptReady:true}}:method==="job_list"?{jobs:[]}:method==="transcript_list"?{playbackUrl:"http://127.0.0.1:1/proxy",sourceMinusProxySeconds:0}:{});
    const bridge={request,shellHealth:vi.fn()} as unknown as DesktopBridge;
    render(<AnalysisPage bridge={bridge} project={project}/>);
    expect(await screen.findByText("Exact sampled frame.")).toBeInTheDocument();
    expect(screen.getByRole("button",{name:"At 1.25s"})).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox",{name:"Detector"}),{target:{value:"repeated_takes"}});
    expect(await screen.findByRole("alert")).toHaveTextContent("result set may be incomplete");
  });
});
