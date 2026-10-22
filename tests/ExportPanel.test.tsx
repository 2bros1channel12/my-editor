import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ExportPanel from "../src/ui/ExportPanel";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse } from "../src/contracts";

describe("export sync provenance", () => {
  it('submits closed finishing settings and an editable interchange export', async()=>{
    const request=vi.fn(async(method:string)=>method==='export_list'?{exports:[]}:method==='export_otio'?{relativePath:'exports/timeline.otio',warnings:['Effects are metadata']}:{id:'job',state:'completed'});
    render(<ExportPanel bridge={{request} as unknown as DesktopBridge} project={{projectId:'p',path:'C:/p'} as LastProjectResponse} hasClips hasCaptions/>);
    fireEvent.change(screen.getByLabelText('Render preset'),{target:{value:'preview'}});
    fireEvent.change(screen.getByLabelText('Target LUFS'),{target:{value:'-18'}});
    fireEvent.click(screen.getByText('Export H.264 MP4'));
    await waitFor(()=>expect(request).toHaveBeenCalledWith('export_generate',expect.objectContaining({options:expect.objectContaining({mode:'preview',loudnessLufs:-18})})));
    fireEvent.click(screen.getByText('Export editable OTIO'));
    await screen.findByText(/Saved exports\/timeline.otio/);
    expect(request).toHaveBeenCalledWith('export_otio',{path:'C:/p'});
  });
  it("shows validated start and end alignment measurements", async () => {
    const bridge = {
      request: vi.fn(async (method: string) => method === "export_list" ? ({ exports: [{
        id: "export-1", format: "mp4-h264-aac", status: "completed", relativePath: "exports/lesson.mp4",
        completedAt: "2026-10-17T12:00:00Z", settings: {
          audioVideoStartAlignmentValidated: true, audioVideoStartOffsetSeconds: 0.012,
          audioVideoEndAlignmentValidated: true, audioVideoEndOffsetSeconds: -0.021,
        },
      }] }) : ({})),
    } as unknown as DesktopBridge;
    const project = { projectId: "project-1", path: "C:/lesson.localcut" } as LastProjectResponse;

    render(<ExportPanel bridge={bridge} project={project} hasClips hasCaptions={false} />);

    const limits = screen.getByRole("complementary", { name: "Current render limits" });
    expect(limits).toHaveTextContent("video clips must be sequential and play at normal speed");
    expect(limits).toHaveTextContent("Overlapping clips, speed changes, overlays, and cross-dissolves are not rendered");
    const row = await screen.findByText(/A\/V start \+12 ms \(within one frame\)/);
    expect(row).toHaveTextContent("A/V end -21 ms (within one frame)");
  });
});
