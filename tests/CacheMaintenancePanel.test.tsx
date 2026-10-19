import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CacheMaintenancePanel from "../src/ui/CacheMaintenancePanel";
import type { DesktopBridge } from "../src/bridge";
import type { LastProjectResponse, ProjectCacheSnapshot } from "../src/contracts";

const project = { projectId: "project-1", path: "C:/work/lesson.localcut", state: {} } as LastProjectResponse;
const snapshot: ProjectCacheSnapshot = {
  projectId: "project-1",
  projectPath: project.path,
  entries: [{ artifactId: "cache-1", kind: "proxy", relativePath: "cache/proxies/job-1", path: "C:/work/lesson.localcut/cache/proxies/job-1", status: "ready", sizeBytes: 4096 }],
  artifactCount: 1,
  totalBytes: 4096,
  activeJobs: [],
  canClear: true,
  confirmationToken: "a".repeat(64),
  protectedPaths: ["media/", "exports/", "models/", "transcripts in SQLite", "project database and history", "recovery/"],
};

describe("project cache maintenance", () => {
  it("shows exact paths and requires a separate confirmation before deletion", async () => {
    const request = vi.fn(async (method: string) => {
      if (method === "project_cache_info") return snapshot;
      if (method === "project_cache_clear") return { clearedCount: 1, clearedBytes: 4096, snapshot: { ...snapshot, entries: [], artifactCount: 0, totalBytes: 0, canClear: false, confirmationToken: null } };
      throw new Error("unexpected method " + method);
    });
    const bridge = { request, shellHealth: async () => ({ available: true, version: "test" }) } as unknown as DesktopBridge;
    render(<CacheMaintenancePanel bridge={bridge} project={project} />);

    fireEvent.click(screen.getByRole("button", { name: "Review project cache" }));
    expect(await screen.findByText(snapshot.entries[0].path)).toBeInTheDocument();
    expect(screen.getByText(/Originals, managed source media/)).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Review deletion confirmation" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Delete the 1 listed generated files");
    expect(request).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Delete listed cache files" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("project_cache_clear", { path: project.path, confirmationToken: snapshot.confirmationToken }));
    expect(await screen.findByRole("status")).toHaveTextContent("Removed 1 items");
  });
});
