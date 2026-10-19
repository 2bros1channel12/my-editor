import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync("src/ui/styles.css", "utf8");

function cssVariable(name: string): string {
  const match = stylesheet.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6}|[0-9.]+rem)`));
  if (!match) throw new Error(`Missing accessibility token ${name}`);
  return match[1]!;
}

function luminance(hex: string): number {
  const channels = hex.slice(1).match(/../g)!.map(value => parseInt(value, 16) / 255);
  const linear = channels.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * linear[0]! + 0.7152 * linear[1]! + 0.0722 * linear[2]!;
}

function contrast(foreground: string, background: string): number {
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0]! + 0.05) / (values[1]! + 0.05);
}

describe("readable interface tokens", () => {
  it("keeps muted and status text above 4.5:1 on light panels", () => {
    const backgrounds = ["#fcfcfa", "#fff9e9", "#f3f7f0"];
    for (const token of ["--text-secondary", "--text-positive", "--text-negative"]) {
      for (const background of backgrounds) expect(contrast(cssVariable(token), background)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("uses rem-based sizes for dense metadata so system text scaling applies", () => {
    expect(cssVariable("--font-meta")).toBe("0.75rem");
    expect(cssVariable("--font-small")).toBe("0.6875rem");
    expect(stylesheet).toContain(".project-card-copy span,.project-path");
    expect(stylesheet).toContain("font-size:var(--font-meta)");
  });
});
