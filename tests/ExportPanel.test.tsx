import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ExportPanel from "../src/ui/ExportPanel";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse } from "../src/contracts";

describe("export sync provenance", () => {
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
