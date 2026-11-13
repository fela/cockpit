import { build } from "esbuild";
import { readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const output = path.resolve(root, "../src/cockpit/static");
const result = await build({
  absWorkingDir: root, entryPoints: ["prompt-editor.js"],
  outfile: path.join(output, "prompt-editor.js"),
  bundle: true, format: "iife", minify: true, target: ["es2022"],
  metafile: true, legalComments: "none",
  banner: { js: "// Generated from frontend/prompt-editor.js. Run npm ci && npm run build in frontend/.\n// Dependency notices: /static/prompt-editor.LICENSE.txt" },
});

// Keep every bundled dependency's complete copyright/license notice alongside
// the minified artifact, including transitive packages. No CDN or Node runtime
// is needed to run Cockpit after this bundle is checked in.
const packages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  if (!input.startsWith("node_modules/")) continue;
  const parts = input.split("/");
  packages.add(parts.slice(0, parts[1].startsWith("@") ? 3 : 2).join("/"));
}
const notices = [];
for (const directory of [...packages].sort()) {
  const absolute = path.join(root, directory);
  const { name, version, license } = JSON.parse(await readFile(path.join(absolute, "package.json"), "utf8"));
  const files = (await readdir(absolute)).filter((name) => /^licen[sc]e(?:\.|$|-)/i.test(name)).sort();
  if (!files.length) throw new Error(`Missing license file for ${name}`);
  const texts = await Promise.all(files.map((file) => readFile(path.join(absolute, file), "utf8")));
  notices.push(`${name} ${version} (${license})\n${texts.join("\n")}`);
}
await writeFile(path.join(output, "prompt-editor.LICENSE.txt"), notices.join("\n---\n\n"));
