import { describe, expect, it, vi } from "vitest";
import { loadHealth, type DesktopBridge } from "../src/bridge";

describe("UI to local service bridge", () => {
  it("loads health over the typed bridge", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockResolvedValue({
        service: { available: true, detail: "Connected", version: "0.1.0" },
        ffmpeg: { available: false, detail: "Not installed" },
        storage: { available: true, detail: "Writable" },
      }),
    };
    const health = await loadHealth(bridge);
    expect(health.ui.available).toBe(true);
    expect(health.service.available).toBe(true);
    expect(health.ffmpeg.available).toBe(false);
    expect(bridge.request).toHaveBeenCalledWith("health");
  });

  it("keeps UI health available when the Python service is down", async () => {
    const bridge: DesktopBridge = {
      shellHealth: vi.fn().mockResolvedValue({ available: true, version: "0.1.0" }),
      request: vi.fn().mockRejectedValue(new Error("service unavailable")),
    };
    const health = await loadHealth(bridge);
    expect(health.ui.available).toBe(true);
    expect(health.service.available).toBe(false);
  });
});
