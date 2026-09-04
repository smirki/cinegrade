/* theme: a real click on #themeToggle flips data-theme on <html>
 * (theme.js). Clicked twice so the run leaves the page in the theme it
 * found it in. */
export default async function run(ctx) {
  const page = ctx.page;

  const need = ["mousedown", "mouseup"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, #themeToggle cannot be clicked for real",
    };
  }

  const before = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  await page.click("#themeToggle");
  await new Promise((r) => setTimeout(r, 100));
  const after = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));

  if (before === after) {
    return { status: "FAIL", evidence: "data-theme stayed " + (before || "(unset, dark)") + " after clicking #themeToggle" };
  }

  await page.click("#themeToggle");
  await new Promise((r) => setTimeout(r, 100));
  const restored = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));

  return {
    status: "PASS",
    evidence: "data-theme " + (before || "(unset, dark)") + " -> " + (after || "(unset, dark)")
      + " on #themeToggle click, back to " + (restored || "(unset, dark)") + " on a second click",
  };
}
