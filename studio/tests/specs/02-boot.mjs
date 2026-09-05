/* boot: zero pageerror events and zero console.error messages between the
 * initial navigation and the app's first render (run.mjs's loadFence and
 * afterLoadFence), and no RangeError anywhere in that window even if it was
 * logged at a level other than error. This is the "the next silent crash on
 * page load is caught" spec: it does not know what a future regression will
 * look like, it only knows a healthy load produces no error output at all.
 *
 * Since contract C4 a boot also has to LAND SOMEWHERE: the page no longer
 * starts on clips[0] with a freshly published default config, it asks the
 * server which project is open and shows that one. So this spec also checks
 * that after a healthy load the server has a project open and the page is
 * showing that project's clip. Without this, "boot restores nothing" would
 * still count as a clean boot. */
import { hasRangeError } from "../lib/util.mjs";

export default async function run(ctx) {
  const consoleSlice = ctx.consoleEvents.slice(ctx.marks.load.console, ctx.marks.afterLoad.console);
  const errorSlice = ctx.pageErrors.slice(ctx.marks.load.pageErrors, ctx.marks.afterLoad.pageErrors);
  const consoleErrors = consoleSlice.filter((e) => e.type === "error");

  ctx.state.bootConsoleErrors = consoleErrors;
  ctx.state.bootPageErrors = errorSlice;

  if (hasRangeError(consoleSlice) || hasRangeError(errorSlice)) {
    const hit = consoleSlice.concat(errorSlice).filter((e) => /RangeError/.test(e.text || e.message || ""))[0];
    return { status: "FAIL", evidence: "a RangeError appeared during load: " + JSON.stringify(hit) };
  }
  if (errorSlice.length) {
    return { status: "FAIL", evidence: errorSlice.length + " pageerror event(s) during load, first: " + errorSlice[0].message };
  }
  if (consoleErrors.length) {
    return {
      status: "FAIL",
      evidence: consoleErrors.length + " console.error message(s) during load, first: \"" + consoleErrors[0].text + "\"",
    };
  }
  const proj = await fetch(ctx.baseUrl + "/api/project").then((r) => r.json());
  if (!proj.open) {
    return { status: "FAIL", evidence: "the load left no project open, so a reload has nothing to come back to" };
  }
  const onScreen = await ctx.page.evaluate(() => {
    const first = document.querySelector("#clipInfo div span");
    return first ? first.textContent : null;
  });
  if (onScreen && proj.name && onScreen !== proj.name) {
    return {
      status: "FAIL",
      evidence: "the open project is " + proj.name + " but the page is showing " + onScreen,
    };
  }
  return {
    status: "PASS",
    evidence: "0 pageerror, 0 console.error, no RangeError across " + consoleSlice.length
      + " console message(s) during load and first render; the load opened project " + proj.head
      + " on " + proj.name + " and the page is showing it",
  };
}
