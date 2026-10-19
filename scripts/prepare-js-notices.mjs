import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
const packages = new Map();
const pending = Object.keys(manifest.dependencies ?? {}).map((name) => ({ name, from: root }));

function packageRoot(name, from) {
  const localRequire = createRequire(path.join(from, "package.json"));
  let entry;
  try {
    entry = localRequire.resolve(name);
  } catch (error) {
    throw new Error(`Production dependency '${name}' cannot be resolved from '${from}'.`, { cause: error });
  }
  let directory = path.dirname(entry);
  while (directory !== path.dirname(directory)) {
    const packageFile = path.join(directory, "package.json");
    if (fs.existsSync(packageFile)) {
      const metadata = JSON.parse(fs.readFileSync(packageFile, "utf8"));
      if (metadata.name === name) return { directory, metadata };
    }
    directory = path.dirname(directory);
  }
  throw new Error(`Could not locate package metadata for '${name}'.`);
}

while (pending.length) {
  const { name, from } = pending.pop();
  let resolved;
  try {
    resolved = packageRoot(name, from);
  } catch (error) {
    // An optional peer may intentionally be absent in the production install.
    if (from !== root && error.cause?.code === "MODULE_NOT_FOUND") continue;
    throw error;
  }
  const key = `${resolved.metadata.name}@${resolved.metadata.version}`;
  if (packages.has(key)) continue;
  if (!resolved.metadata.license) throw new Error(`Production dependency '${key}' has no declared license.`);
  packages.set(key, resolved);
  for (const dependency of Object.keys({
    ...resolved.metadata.dependencies,
    ...resolved.metadata.optionalDependencies,
    ...resolved.metadata.peerDependencies,
  })) {
    pending.push({ name: dependency, from: resolved.directory });
  }
}

const notices = [
  "Local Cut JavaScript production dependency notices",
  "Generated from the installed production dependency graph before each Tauri release build.",
  "",
];
const packageRecords = [...packages.entries()].sort(([left], [right]) => left.localeCompare(right));

for (const [identity, { directory, metadata }] of packageRecords) {
  const files = fs.readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile() && /^(LICENSE|LICENCE|COPYING|NOTICE)(\.|-|$)/i.test(entry.name))
    .map((entry) => entry.name)
    .sort();
  if (!files.length) throw new Error(`Production dependency '${identity}' declares ${metadata.license} but ships no license or notice file.`);
  notices.push("=".repeat(78), `${identity}`, `Declared license: ${metadata.license}`, `Package: ${metadata.homepage ?? metadata.repository?.url ?? "homepage not declared"}`, "");
  for (const filename of files) {
    const body = fs.readFileSync(path.join(directory, filename), "utf8").trimEnd();
    notices.push(`--- ${filename} ---`, body, "");
  }
}

const outputPath = path.join(root, "resources", "licenses", "JavaScript-THIRD-PARTY.txt");
fs.mkdirSync(path.dirname(outputPath), { recursive: true });
fs.writeFileSync(outputPath, `${notices.join("\n")}\n`, "utf8");
console.log(`Prepared notices for ${packages.size} production JavaScript packages at ${path.relative(root, outputPath)}.`);
