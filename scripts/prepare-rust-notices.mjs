import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifestPath = path.join(root, "src-tauri", "Cargo.toml");
const overrideManifestPath = path.join(root, "resources", "license-overrides", "rust-license-overrides.json");
const target = "x86_64-pc-windows-msvc";
const licenseOverrides = JSON.parse(fs.readFileSync(overrideManifestPath, "utf8"));
const cargoCandidates = [process.env.CARGO, "cargo", path.join(os.homedir(), ".cargo", "bin", "cargo.exe")].filter(Boolean);
let cargo;
for (const candidate of cargoCandidates) {
  try {
    execFileSync(candidate, ["--version"], { stdio: "ignore" });
    cargo = candidate;
    break;
  } catch { }
}
if (!cargo) throw new Error("Cargo is required to prepare the Windows Rust dependency notices.");

function runCargo(args) {
  try {
    return execFileSync(cargo, args, { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, stdio: ["ignore", "pipe", "inherit"] });
  } catch (error) {
    throw new Error(`Cargo ${args[0]} failed while preparing dependency notices.`, { cause: error });
  }
}

runCargo(["fetch", "--locked", "--target", target, "--manifest-path", manifestPath]);
const metadata = JSON.parse(runCargo(["metadata", "--locked", "--format-version", "1", "--manifest-path", manifestPath]));
const tree = runCargo(["tree", "--locked", "--target", target, "--edges", "normal,build", "--prefix", "none", "--format", "{p}", "--manifest-path", manifestPath]);
const reachable = new Set();
for (const line of tree.split(/\r?\n/)) {
  const match = line.match(/^(.+?) v([^\s(]+)(?:\s|$)/);
  if (match) reachable.add(`${match[1]}@${match[2]}`);
}
const workspacePackages = new Set(metadata.packages
  .filter((pkg) => metadata.workspace_members.includes(pkg.id))
  .map((pkg) => `${pkg.name}@${pkg.version}`));
for (const entry of workspacePackages) reachable.delete(entry);

const selected = metadata.packages
  .filter((pkg) => pkg.source && reachable.has(`${pkg.name}@${pkg.version}`))
  .sort((left, right) => `${left.name}@${left.version}`.localeCompare(`${right.name}@${right.version}`));
if (!selected.length) throw new Error("Cargo did not report any resolved Windows dependencies.");
const crateIds = new Set(selected.map((pkg) => `${pkg.name}@${pkg.version}`));
for (const entry of reachable) {
  if (!crateIds.has(entry)) throw new Error(`Cargo tree dependency '${entry}' has no matching metadata entry.`);
}

const notices = [
  `Local Cut Rust dependency notices for ${target}`,
  "Generated from the locked Cargo graph used by the Windows release build (normal and build dependencies; development dependencies excluded).",
  "",
];
const legalFile = /^(LICENSE|LICENCE|COPYING|NOTICE)(\.|-|$)/i;
const missingText = [];
const supplementedText = [];

function collectLegalFiles(directory, depth = 0, relative = "") {
  const result = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.isFile() && legalFile.test(entry.name)) result.push(path.join(relative, entry.name));
    else if (entry.isDirectory() && depth < 2 && ["licenses", "license", "licenses"].includes(entry.name.toLowerCase())) {
      result.push(...collectLegalFiles(path.join(directory, entry.name), depth + 1, path.join(relative, entry.name)));
    }
  }
  return result.sort();
}

for (const pkg of selected) {
  const crateRoot = path.dirname(pkg.manifest_path);
  const files = new Set(collectLegalFiles(crateRoot));
  if (pkg.license_file) {
    const declaredFile = path.resolve(crateRoot, pkg.license_file);
    const relative = path.relative(crateRoot, declaredFile);
    if (relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error(`Crate '${pkg.name}@${pkg.version}' declares a license file outside its source directory.`);
    if (!fs.existsSync(declaredFile) || !fs.statSync(declaredFile).isFile()) throw new Error(`Crate '${pkg.name}@${pkg.version}' is missing its declared license file.`);
    files.add(relative);
  }
  if (!pkg.license) throw new Error(`Crate '${pkg.name}@${pkg.version}' has no declared SPDX license expression.`);
  const packageId = `${pkg.name}@${pkg.version}`;
  let supplemental = [];
  if (!files.size && licenseOverrides[packageId]) {
    const override = licenseOverrides[packageId];
    if (override.license !== pkg.license) throw new Error(`License override for '${packageId}' does not match Cargo metadata.`);
    const vcsPath = path.join(crateRoot, ".cargo_vcs_info.json");
    const vcs = JSON.parse(fs.readFileSync(vcsPath, "utf8"));
    if (vcs.git?.sha1 !== override.sourceRevision || vcs.path_in_vcs !== override.sourcePath) {
      throw new Error(`License override for '${packageId}' does not match its locked upstream source revision.`);
    }
    const packageManifest = fs.readFileSync(pkg.manifest_path, "utf8");
    if (!packageManifest.includes(`repository = "${override.repository}"`)) {
      throw new Error(`License override for '${packageId}' does not match the source repository declared in Cargo metadata.`);
    }
    supplemental = override.files.map((entry) => {
      const absolute = path.resolve(root, "resources", "license-overrides", entry.path);
      const relative = path.relative(path.join(root, "resources", "license-overrides"), absolute);
      if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) || !fs.existsSync(absolute) || !fs.statSync(absolute).isFile()) {
        throw new Error(`Supplemental license text for '${packageId}' is missing or outside the override directory.`);
      }
      const digest = crypto.createHash("sha256").update(fs.readFileSync(absolute)).digest("hex");
      if (digest !== entry.sha256) throw new Error(`Supplemental license text checksum mismatch for '${packageId}': ${entry.path}`);
      return { ...entry, absolute };
    });
    if (!supplemental.length) throw new Error(`License override for '${packageId}' has no supplemental texts.`);
    supplementedText.push(packageId);
  } else if (!files.size) {
    missingText.push(`${packageId} (${pkg.license})`);
  }
  notices.push("=".repeat(78), `${pkg.name}@${pkg.version}`, `Declared license: ${pkg.license}`, `Source: ${pkg.source ?? "workspace"}`, "");
  for (const relative of [...files].sort()) {
    const absolute = path.join(crateRoot, relative);
    const body = fs.readFileSync(absolute, "utf8").trimEnd();
    notices.push(`--- ${relative.replaceAll(path.sep, "/")} ---`, body, "");
  }
  for (const entry of supplemental) {
    notices.push(`Supplemental license source: ${entry.source}`, `--- license-overrides/${entry.path} (SHA-256 ${entry.sha256}) ---`, fs.readFileSync(entry.absolute, "utf8").trimEnd(), "");
  }
  if (!files.size) notices.push("The locked crate declares this license expression in Cargo metadata but its source package includes no separate license or notice text file. Review the crate's attribution and distribution terms before release.", "");
}

notices.splice(2, 0,
  `Packages without license/notice text after reviewed overrides: ${missingText.length} of ${selected.length}.`,
  ...(missingText.length ? [missingText.join("\n"), ""] : []),
  `Packages using version-pinned supplemental license texts: ${supplementedText.length} of ${selected.length}.`,
  ...(supplementedText.length ? [supplementedText.join("\n"), ""] : []),
);

const outputPath = path.join(root, "resources", "licenses", "Rust-THIRD-PARTY.txt");
fs.mkdirSync(path.dirname(outputPath), { recursive: true });
fs.writeFileSync(outputPath, `${notices.join("\n")}\n`, "utf8");
console.log(`Prepared notices for ${selected.length} locked Windows Rust packages (${missingText.length} source packages had no separate license/notice text file) at ${path.relative(root, outputPath)}.`);
