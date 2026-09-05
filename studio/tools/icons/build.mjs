#!/usr/bin/env node
/* Vendors a fixed list of HugeIcons definitions into studio/static/vendor/hugeicons.js.
 *
 * The list below is the only place a human edits: add or remove a name, run
 * `npm install && node build.mjs` in this folder, and the vendored file is
 * regenerated from the installed package. No path data is ever typed or
 * edited by hand anywhere downstream of this script: studio/static/icons.js
 * only reads window.HUGEICONS and turns each definition's [tag, attrs] pairs
 * into real SVG nodes at runtime.
 *
 * Package: @hugeicons/core-free-icons (MIT, Copyright 2025 Hugeicons).
 * Regenerate with: cd studio/tools/icons && npm install && node build.mjs
 */

import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const OUT_PATH = join(HERE, "..", "..", "static", "vendor", "hugeicons.js");

// One name per icon actually used by the studio (index.html, panels.js,
// app.js). Every name here is the package's own export name, unchanged.
const ICON_NAMES = [
  "ChevronDownIcon",
  "RefreshCwIcon",
  "FilterHorizontalIcon",
  "EaseCurveControlPointsIcon",
  "ColorPickerIcon",
  "Film01Icon",
  "SparklesIcon",
  "ChartScatterIcon",
  "FocusIcon",
  "AspectRatioIcon",
  "Download01Icon",
  "EllipseSelectionIcon",
  "ArrowUp01Icon",
  "Folder01Icon",
  "PlaySquareIcon",
  "Home03Icon",
  "LayoutAlignLeftIcon",
  "LayoutAlignRightIcon",
  "Sun03Icon",
  "Moon02Icon",
  "Layers01Icon",
  "Add01Icon",
  "Copy01Icon",
  "ArrowDown01Icon",
  "Delete01Icon"
];

const pkgJsonPath = require.resolve("@hugeicons/core-free-icons/package.json");
const pkgJson = JSON.parse(require("node:fs").readFileSync(pkgJsonPath, "utf8"));
const pkgVersion = pkgJson.version;
const pkgLicense = pkgJson.license;

const definitions = {};
for (const name of ICON_NAMES) {
  // Deep import of the package's own per-icon ESM file, one file per name
  // (the package ships one module per icon, see its own dist/esm/*.js);
  // nothing here re-derives or edits the shape, it is imported verbatim.
  const mod = await import(`@hugeicons/core-free-icons/${name}`);
  const def = mod.default;
  if (!Array.isArray(def) || !def.length) {
    throw new Error(`Icon "${name}" did not resolve to a non-empty array from @hugeicons/core-free-icons`);
  }
  definitions[name] = def;
}

const header = `/* Vendored from @hugeicons/core-free-icons v${pkgVersion} (MIT licence,
 * Copyright 2025 Hugeicons). Generated file, not hand-edited: every
 * definition below is the package's own [tag, attributes] array, copied
 * verbatim by studio/tools/icons/build.mjs, which is the only place a human
 * chooses icon names. Regenerate with:
 *   cd studio/tools/icons && npm install && node build.mjs
 *
 * Plain classic (non-module) script: defines window.HUGEICONS so index.html
 * can load it with a bare <script src> ahead of icons.js, no build step or
 * bundler needed at runtime, matching every other file under
 * studio/static/vendor.
 */
window.HUGEICONS = ${JSON.stringify(definitions, null, 2)};
`;

writeFileSync(OUT_PATH, header, "utf8");
console.log(`Wrote ${OUT_PATH}`);
console.log(`@hugeicons/core-free-icons version ${pkgVersion}, licence ${pkgLicense}`);
console.log(`${ICON_NAMES.length} icons: ${ICON_NAMES.join(", ")}`);
