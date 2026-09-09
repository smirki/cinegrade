/* The mask model v2 arithmetic reference, run as part of the suite.
 *
 * studio/tests/mask-stack-ref.mjs loads the real gpu.js in a `vm` (no browser,
 * no server, no GPU) and asserts on StudioGPU.mask.*, which is the DEFINITION
 * the shaders and the engine are both ports of: the fold from zero, the three
 * ops, invert before feather, the finesse order, the two roundings, the frame
 * index and the frame cache.
 *
 * It exists as its own file because it needs none of this harness's machinery.
 * It was ALSO run by nothing at all: not by run.mjs, not by a package.json
 * script, not by the parity gate, and the arc's report counted its checks as a
 * green gate on the strength of a hand run. This spec is the wiring. It shells
 * out to the same file a person would run by hand, with this harness's own
 * interpreter, and turns its exit code and its count line into a spec result,
 * so a broken assertion in there fails `node run.mjs` like any other spec.
 *
 * Deliberately NOT a copy of those assertions: one definition of the mask
 * arithmetic, in one file, run from two places.
 */

import { execFile } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REF = path.resolve(HERE, "..", "mask-stack-ref.mjs");

function run(cmd, args) {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout: 120000, encoding: "utf8" },
      (err, stdout, stderr) => {
        resolve({ code: err ? (err.code === undefined ? 1 : err.code) : 0,
                  stdout: stdout || "", stderr: stderr || "" });
      });
  });
}

export default async function run_(ctx) {
  const r = await run(process.execPath, [REF]);
  const out = (r.stdout + r.stderr).trim();
  // The file's last word on itself: "PASS  163 checks passed, 0 failed".
  const line = out.split("\n").filter((l) => /checks passed/.test(l))[0] || "";
  const m = /(PASS|FAIL)\s+(\d+) checks passed, (\d+) failed/.exec(line);
  if (!m) {
    return { status: "FAIL",
             evidence: "mask-stack-ref.mjs printed no count line (exit "
               + r.code + "): " + out.split("\n").slice(-4).join(" | ") };
  }
  const passed = Number(m[2]), failed = Number(m[3]);
  if (r.code !== 0 || failed > 0 || m[1] !== "PASS") {
    const bad = out.split("\n").filter((l) => /^\s+FAIL/.test(l)).slice(0, 6);
    return { status: "FAIL",
             evidence: failed + " of " + (passed + failed)
               + " mask reference checks failed (exit " + r.code + "): "
               + bad.join(" | ") };
  }
  /* A floor on the count as well as on the verdict. An empty or half loaded
   * gpu.js would exit 0 with almost nothing checked, and "PASS" on four checks
   * reads exactly like "PASS" on a hundred and sixty in a table.
   *
   * The floor is the EXACT live count, the way sam/tests/run.py sets its
   * floors (round 2 finding 73). At 150 against 191 live checks, forty one
   * of them could have been deleted with the runner none the wiser, which is
   * the same hole this floor exists to close. Raise it whenever the file
   * legitimately grows: that edit is the point. */
  if (passed < 191) {
    return { status: "FAIL",
             evidence: "only " + passed + " mask reference checks ran, expected "
               + "at least 191: something stopped gpu.js from loading fully, "
               + "or checks were deleted" };
  }
  return { status: "PASS",
           evidence: passed + " mask arithmetic reference checks passed "
             + "(studio/tests/mask-stack-ref.mjs, no browser and no GPU)" };
}
