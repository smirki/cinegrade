/* reload-clean: reloads the page and repeats the boot spec's check (zero
 * pageerror, zero console.error, no RangeError anywhere) against a fresh
 * navigation, fenced independently of the initial load and of every other
 * spec's own console/page-error output in between.
 *
 * Since contract C4 "clean" also means QUIET: a reload reads the open project
 * and shows it, so it must not write. This spec pins the two cheap proofs of
 * that from the server's side, no page instrumentation needed: the project's
 * commit log is the same length afterwards (the reload committed nothing) and
 * the live session's revision is unchanged (the reload published nothing).
 * That is the "if i reload it shouldnt break or reset the whole application"
 * requirement measured at its narrowest. Spec 19 checks the other half, that
 * what comes back is what was there. */
import { hasRangeError } from "../lib/util.mjs";

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;

  const fenceBefore = { console: ctx.consoleEvents.length, pageErrors: ctx.pageErrors.length };
  const projBefore = await fetch(base + "/api/project").then((r) => r.json());
  const logBefore = projBefore.open
    ? await fetch(base + "/api/project/log").then((r) => r.json())
    : { commits: [] };
  const revBefore = (await fetch(base + "/api/session").then((r) => r.json())).rev;

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
  if (projBefore.open) {
    const projAfter = await fetch(base + "/api/project").then((r) => r.json());
    const logAfter = await fetch(base + "/api/project/log").then((r) => r.json());
    const revAfter = (await fetch(base + "/api/session").then((r) => r.json())).rev;
    if (!projAfter.open || projAfter.key !== projBefore.key) {
      return {
        status: "FAIL",
        evidence: "the reload changed which project is open, from " + projBefore.name + " to "
          + (projAfter.open ? projAfter.name : "nothing"),
      };
    }
    if (projAfter.head !== projBefore.head) {
      return { status: "FAIL", evidence: "the reload moved HEAD from " + projBefore.head + " to " + projAfter.head };
    }
    if (logAfter.commits.length !== logBefore.commits.length) {
      return {
        status: "FAIL",
        evidence: "the reload wrote " + (logAfter.commits.length - logBefore.commits.length)
          + " commit(s), it must write none",
      };
    }
    if (revAfter !== revBefore) {
      return {
        status: "FAIL",
        evidence: "the reload bumped the session revision from " + revBefore + " to " + revAfter
          + ", so it published something",
      };
    }
  }
  return {
    status: "PASS",
    evidence: "0 pageerror, 0 console.error, no RangeError across " + consoleSlice.length
      + " console message(s) on a fresh reload; project " + projBefore.head + " still open with "
      + logBefore.commits.length + " commit(s) and session revision still " + revBefore,
  };
}
