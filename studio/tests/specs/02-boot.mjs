/* boot: zero pageerror events and zero console.error messages between the
 * initial navigation and the app's first render (run.mjs's loadFence and
 * afterLoadFence), and no RangeError anywhere in that window even if it was
 * logged at a level other than error. This is the "the next silent crash on
 * page load is caught" spec: it does not know what a future regression will
 * look like, it only knows a healthy load produces no error output at all. */
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
  return {
    status: "PASS",
    evidence: "0 pageerror, 0 console.error, no RangeError across " + consoleSlice.length + " console message(s) during load and first render",
  };
}
