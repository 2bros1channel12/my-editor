import { describe, expect, it } from "vitest";
import protocol from "../contracts/protocol.schema.json";
import { readFileSync } from "node:fs";
import type { DesktopBridge } from "../src/bridge";
import { SERVICE_METHODS, type AnalysisDetector, type ServiceHealth } from "../src/contracts";

// This function is intentionally type-checked but never invoked. It makes the
// request and response map part of the normal `tsc -b` verification.
export function bridgeContractCompileChecks(bridge: DesktopBridge) {
  const health: Promise<ServiceHealth> = bridge.request("health");
  const detectors: Promise<{ detectors: AnalysisDetector[] }> = bridge.request("analysis_catalog");
  // @ts-expect-error analysis_run requires an asset ID as well as its path and detector.
  void bridge.request("analysis_run", { path: "project", detector: "silence" });
  // @ts-expect-error range markers are limited to the three review states.
  void bridge.request("transcript_marker", { path: "project", transcriptId: "t", sourceInTicks: 0, sourceOutTicks: 1, marker: "remove" });
  return Promise.all([health, detectors]);
}

describe("desktop/service protocol contracts", () => {
  it("keeps the TypeScript method map aligned with the Python stdio schema", () => {
    const requestProperties = protocol.oneOf[0]!.properties as { method: { enum: string[] } };
    const schemaMethods = requestProperties.method.enum;
    expect([...schemaMethods].sort()).toEqual([...SERVICE_METHODS].sort());
  });

  it("keeps the Tauri method allowlist aligned with the typed bridge", () => {
    const rust = readFileSync("src-tauri/src/service.rs", "utf8");
    const block = rust.match(/pub const SUPPORTED_METHODS: &\[&str\] = &\[(.*?)\];/s)?.[1];
    expect(block).toBeTruthy();
    const rustMethods = Array.from(block!.matchAll(/"([^"]+)"/g), match => match[1]);
    expect(rustMethods.sort()).toEqual([...SERVICE_METHODS].sort());
  });
});
