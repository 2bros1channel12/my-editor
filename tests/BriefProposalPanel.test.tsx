import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import BriefProposalPanel from "../src/ui/BriefProposalPanel";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse } from "../src/contracts";

const brief={id:"b1",revision:1,briefText:"Audience: developers",constraints:{audience:"developers",objective:"Explain caches",tone:"clear",targetDurationMinutes:null,aspectRatio:"source",mustKeep:["invalidation"],mustRemove:[],preferredOrder:[]},createdAt:"now"};
const operation={id:"s1",type:"include_source_range" as const,assetId:"a1",sourceInTicks:1_000_000,sourceOutTicks:2_000_000,text:"Cache invalidation",status:"pending" as const,confidence:.9,rationale:"Keep this passage."};
const project={projectId:"p1",path:"C:/project",state:{projectId:"p1",revision:4,settings:{},assets:[{id:"a1",original_uri:"lesson.mov"}],timelineItems:[],captions:[],transcripts:[],analyses:[]}} as LastProjectResponse;

describe("creative brief proposal review",()=>{
  it("discards an old project's delayed parsed draft after opening another project", async () => {
    let resolveParse: (value: unknown) => void = () => undefined;
    const request = vi.fn(async (method: string) => {
      if (method === "brief_parse") return new Promise(resolve => { resolveParse = resolve; });
      if (method === "brief_list") return { briefs: [] };
      if (method === "proposal_list") return { plans: [] };
      if (method === "transcript_list") return { transcripts: [] };
      if (method === "style_recipe_catalog") return { recipes: [] };
      return { jobs: [] };
    });
    const bridge = { request, shellHealth: vi.fn() } as unknown as DesktopBridge;
    const view = render(<BriefProposalPanel bridge={bridge} project={project} onProjectRefresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Creative brief"), { target: { value: "Goal: Previous project" } });
    fireEvent.click(screen.getByRole("button", { name: "Parse locally" }));
    view.rerender(<BriefProposalPanel bridge={bridge} project={{ ...project, projectId: "other", path: "C:/other" }} onProjectRefresh={vi.fn()} />);
    await act(async () => resolveParse({ constraints: { ...brief.constraints, objective: "Previous project" } }));
    expect(screen.getByLabelText("Video purpose")).toHaveValue("");
    expect(screen.getByLabelText("Creative brief")).toHaveValue("");
  });

  it("parses and saves editable constraints before queuing a proposal",async()=>{
    const request=vi.fn(async(method:string)=>{
      if(method==="brief_parse") return {constraints:brief.constraints,parserId:"deterministic",parserVersion:"1"};
      if(method==="brief_save") return {brief};
      if(method==="brief_list") return {briefs:[brief]};
      if(method==="proposal_list") return {plans:[]};
      if(method==="job_list") return {jobs:[]};
      if(method==="transcript_list") return {transcripts:[{id:"stale",assetId:"a1",language:"en",status:"stale",modelId:"old",words:[]},{id:"t1",assetId:"a1",language:"en",status:"ready",modelId:"small",words:[{id:"w1"}]}]};
      if(method==="proposal_generate") return {id:"j1",state:"queued"};
      return {};
    });
    render(<BriefProposalPanel bridge={{request,shellHealth:vi.fn()} as unknown as DesktopBridge} project={project} onProjectRefresh={vi.fn().mockResolvedValue(undefined)}/>);
    fireEvent.change(screen.getByRole("textbox",{name:"Creative brief"}),{target:{value:"Audience: developers"}});
    fireEvent.click(screen.getByRole("button",{name:"Parse locally"}));
    expect(await screen.findByDisplayValue("Explain caches")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("cannot be used for a new proposal");
    expect(screen.queryByRole("option",{name:/old/})).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox",{name:"I reviewed and edited these neutral editing traits"}));
    fireEvent.click(screen.getByRole("button",{name:"Save brief"}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith("brief_save",{path:"C:/project",briefText:"Audience: developers",constraints:expect.objectContaining(brief.constraints),styleReviewed:true}));
    fireEvent.change(await screen.findByRole("combobox",{name:"Transcript"}),{target:{value:"t1"}});
    fireEvent.click(screen.getByRole("button",{name:"Propose first cut"}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith("proposal_generate",{path:"C:/project",assetId:"a1",briefId:"b1",transcriptId:"t1"}));
  });

  it("requires explicit acceptance and refreshes the project revision",async()=>{
    const request=vi.fn(async(method:string)=>method==="brief_list"?{briefs:[brief]}:method==="proposal_list"?{plans:[{id:"p-plan",briefId:"b1",revision:1,status:"pending",plan:{assetId:"a1",operations:[operation],warnings:[],styleGuidance:{trait:"pacing",value:"fast",origin:"reference-derived",generatorId:"localcut-neutral-reference-traits",generatorVersion:"1",gapThresholdSeconds:.7,explanation:"Natural pauses of 0.7s or longer may separate passages."},punchInGuidance:{trait:"punchInFrequencyPerMinute",value:2,origin:"reference-derived",generatorId:"localcut-neutral-reference-traits",generatorVersion:"1",maximumSuggestions:1,explanation:"One protected passage may receive a punch-in suggestion."},noAutomaticChanges:true},createdAt:"now"}]}:method==="job_list"?{jobs:[]}:method==="transcript_list"?{transcripts:[]}:{});
    const refresh=vi.fn().mockResolvedValue(undefined);
    render(<BriefProposalPanel bridge={{request,shellHealth:vi.fn()} as unknown as DesktopBridge} project={project} onProjectRefresh={refresh}/>);
    const guidanceNotes=await screen.findAllByRole("note");
    expect(guidanceNotes[0]).toHaveTextContent("Saved neutral pacing preference: fast");
    expect(guidanceNotes[0]).toHaveTextContent("localcut-neutral-reference-traits v1");
    expect(guidanceNotes[1]).toHaveTextContent("Saved neutral punch-in frequency: 2/min");
    fireEvent.click(await screen.findByRole("button",{name:"Accept suggestion"}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith("proposal_decide",{path:"C:/project",planId:"p-plan",suggestionId:"s1",decision:"accepted",expectedRevision:4,createCaptions:true}));
    expect(refresh).toHaveBeenCalled();
  });
  it("saves a guided-only brief after trait review and never saves inspiration names",async()=>{
    const request=vi.fn(async(method:string)=>{
      if(method==="brief_list") return {briefs:[]};
      if(method==="proposal_list") return {plans:[]};
      if(method==="job_list") return {jobs:[]};
      if(method==="transcript_list") return {transcripts:[]};
      if(method==="style_recipe_catalog") return {recipes:[{id:"fast-educational",name:"Fast educational"}]};
      if(method==="style_recipe_propose") return {recipeId:"fast-educational",recipeVersion:"1",traits:{pacing:"fast",averageShotLengthSeconds:5,jumpCutFrequencyPerMinute:8,captionDensityPerMinute:12,punchInFrequencyPerMinute:2,musicEnergy:"moderate",humorLevel:"low",narrativeStructure:"hook-explanation-summary"},requiresReview:true,explanation:"Generic editable starting point",generatorId:"recipes",generatorVersion:"1"};
      if(method==="brief_save") return {brief};
      return {};
    });
    render(<BriefProposalPanel bridge={{request,shellHealth:vi.fn()} as unknown as DesktopBridge} project={project} onProjectRefresh={vi.fn().mockResolvedValue(undefined)}/>);
    fireEvent.change(screen.getByRole("textbox",{name:"Video purpose"}),{target:{value:"Teach reliable imports"}});
    fireEvent.change(screen.getByRole("textbox",{name:"Required topics or moments (one per line)"}),{target:{value:"pricing, including discounts\nrecovery"}});
    fireEvent.change(screen.getByRole("textbox",{name:"Desired opening"}),{target:{value:"Show finished result"}});
    fireEvent.change(screen.getByRole("textbox",{name:"Optional creator inspiration"}),{target:{value:"Example Creator"}});
    fireEvent.click(screen.getByRole("button",{name:"Propose neutral traits"}));
    await screen.findByText("Generic editable starting point");
    expect(screen.getByDisplayValue("Teach reliable imports")).toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Save brief"})).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox",{name:"I reviewed and edited these neutral editing traits"}));
    fireEvent.change(screen.getByRole("spinbutton",{name:"Cut frequency (per minute)"}),{target:{value:"0"}});
    expect(screen.getByRole("button",{name:"Save brief"})).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox",{name:"I reviewed and edited these neutral editing traits"}));
    fireEvent.click(screen.getByRole("button",{name:"Save brief"}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith("brief_save",expect.objectContaining({briefText:"",styleReviewed:true,constraints:expect.objectContaining({objective:"Teach reliable imports",desiredOpening:"Show finished result",mustKeep:["pricing, including discounts","recovery"],recipeId:"fast-educational",styleTraits:expect.objectContaining({pacing:"fast",jumpCutFrequencyPerMinute:0})})})));
    const saved=request.mock.calls.find(call=>call[0]==="brief_save");
    expect(JSON.stringify(saved)).not.toContain("Example Creator");
    expect(request).toHaveBeenCalledWith("style_recipe_propose",{recipeId:"fast-educational",inspiration:"Example Creator"});
  });

  it("loads old briefs with editable defaults and resets review when the project changes",async()=>{
    const request=vi.fn(async(method:string)=>method==="brief_list"?{briefs:[brief]}:method==="proposal_list"?{plans:[]}:method==="job_list"?{jobs:[]}:method==="transcript_list"?{transcripts:[]}:{});
    const bridge={request,shellHealth:vi.fn()} as unknown as DesktopBridge;
    const {rerender}=render(<BriefProposalPanel bridge={bridge} project={project} onProjectRefresh={vi.fn().mockResolvedValue(undefined)}/>);
    await screen.findByRole("option",{name:/Revision 1/});
    fireEvent.change(screen.getByRole("combobox",{name:"Saved brief"}),{target:{value:"b1"}});
    expect(screen.getByDisplayValue("Explain caches")).toBeInTheDocument();
    expect(screen.getByRole("combobox",{name:"Platform"})).toHaveValue("unspecified");
    fireEvent.click(screen.getByRole("checkbox",{name:"I reviewed and edited these neutral editing traits"}));
    expect(screen.getByRole("button",{name:"Save brief"})).toBeEnabled();
    rerender(<BriefProposalPanel bridge={bridge} project={{...project,projectId:"p2",path:"C:/other"}} onProjectRefresh={vi.fn().mockResolvedValue(undefined)}/>);
    await waitFor(()=>expect(screen.getByRole("textbox",{name:"Video purpose"})).toHaveValue(""));
    expect(screen.getByRole("checkbox",{name:"I reviewed and edited these neutral editing traits"})).not.toBeChecked();
    expect(screen.getByRole("button",{name:"Save brief"})).toBeDisabled();
  });

  it("adopts parsed style traits into the editable draft and saved planner constraints",async()=>{
    const parsedTraits={pacing:"fast",averageShotLengthSeconds:null,jumpCutFrequencyPerMinute:7,captionDensityPerMinute:0,punchInFrequencyPerMinute:null,musicEnergy:"low",humorLevel:"moderate",narrativeStructure:"step-by-step"};
    const request=vi.fn(async(method:string)=>{
      if(method==="brief_parse") return {constraints:{...brief.constraints,styleTraits:parsedTraits}};
      if(method==="brief_list") return {briefs:[]};
      if(method==="proposal_list") return {plans:[]};
      if(method==="job_list") return {jobs:[]};
      if(method==="transcript_list") return {transcripts:[]};
      if(method==="brief_save") return {brief};
      return {};
    });
    render(<BriefProposalPanel bridge={{request,shellHealth:vi.fn()} as unknown as DesktopBridge} project={project} onProjectRefresh={vi.fn().mockResolvedValue(undefined)}/>);
    fireEvent.change(screen.getByRole("textbox",{name:"Creative brief"}),{target:{value:"Objective: Explain caches\nPacing: fast\nCut frequency: 7"}});
    fireEvent.click(screen.getByRole("button",{name:"Parse locally"}));
    await waitFor(()=>expect(screen.getByRole("combobox",{name:"Pacing"})).toHaveValue("fast"));
    expect(screen.getByRole("spinbutton",{name:"Caption density (cues per minute)"})).toHaveValue(0);
    expect(screen.getByRole("button",{name:"Save brief"})).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox",{name:"I reviewed and edited these neutral editing traits"}));
    fireEvent.click(screen.getByRole("button",{name:"Save brief"}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith("brief_save",expect.objectContaining({constraints:expect.objectContaining({styleTraits:parsedTraits}),styleReviewed:true})));
  });

  it("preserves local reference provenance through the main save action",async()=>{
    const traits={pacing:"fast",averageShotLengthSeconds:4,jumpCutFrequencyPerMinute:8,captionDensityPerMinute:null,punchInFrequencyPerMinute:null,musicEnergy:"unknown",humorLevel:"unknown",narrativeStructure:"unknown"};
    const request=vi.fn(async(method:string)=>{
      if(method==="brief_list") return {briefs:[]};
      if(method==="proposal_list") return {plans:[]};
      if(method==="job_list") return {jobs:[]};
      if(method==="transcript_list") return {transcripts:[]};
      if(method==="reference_analyze") return {generatorId:"localcut-neutral-reference-traits",generatorVersion:"1",traits,sampleFrames:5,analyzedSeconds:20,explanation:"Measured local traits",ephemeral:true};
      if(method==="brief_save") return {brief};
      return {};
    });
    render(<BriefProposalPanel bridge={{request,selectReferenceInput:vi.fn().mockResolvedValue("C:/reference.mov"),shellHealth:vi.fn()} as unknown as DesktopBridge} project={project} onProjectRefresh={vi.fn().mockResolvedValue(undefined)}/>);
    fireEvent.change(screen.getByRole("textbox",{name:"Video purpose"}),{target:{value:"Explain caches"}});
    fireEvent.click(screen.getByRole("button",{name:"Analyze reference"}));
    await waitFor(()=>expect(screen.getByRole("combobox",{name:"Pacing"})).toHaveValue("fast"));
    fireEvent.click(screen.getByRole("checkbox",{name:"I reviewed and edited these neutral editing traits"}));
    fireEvent.click(screen.getByRole("button",{name:"Save brief"}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith("brief_save",expect.objectContaining({traits,traitOrigin:"reference-derived",styleReviewed:true,constraints:expect.objectContaining({styleTraits:traits})})));
  });

});
