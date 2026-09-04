/* reload-clean: reloads the page and repeats the boot spec's check (zero
 * pageerror, zero console.error, no RangeError anywhere) against a fresh
 * navigation, fenced independently of the initial load and of every other
 * spec's own console/page-error output in between. */
import { hasRangeError } from "../lib/util.mjs";

export default async function run(ctx) {
  const page = ctx.page;

  const fenceBefore = { console: ctx.consoleEvents.length, pageErrors: ctx.pageErrors.length };

  await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await new Promise((r) => setTimeout(r, 500));

  const fenceAfter = { console: ctx.consoleEvents.length, pageErrors: ctx.pageErrors.length };
  const consoleSlice = ctx.consoleEvents.slice(fenceBefore.console, fenceAfter.console);
  const errorSlice = ctx.pageErrors.slice(fenceBefore.pageErrors, fenceAfter.pageErrors);
  const consoleErrors = consoleSlice.filter((e) => e.type === "error");

  if (hasRangeError(consoleSlice) || hasRangeError(errorSlice)) {
    return { status: "FAIL", evidence: "a RangeError appeared on reload" };
  }
  if (errorSlice.length) {
    return { status: "FAIL", evidence: errorSlice.length + " pageerror event(s) on reload, first: " + errorSlice[0].message };
  }
  if (consoleErrors.length) {
    return {
      status: "FAIL",
      evidence: consoleErrors.length + " console.error message(s) on reload, first: \"" + consoleErrors[0].text + "\"",
    };
  }
  return {
    status: "PASS",
    evidence: "0 pageerror, 0 console.error, no RangeError across " + consoleSlice.length + " console message(s) on a fresh reload",
  };
}
