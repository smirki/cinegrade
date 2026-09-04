/* panels: every section in schema.js is rendered inside #params. panels.js
 * builds one <section class="stage" data-stage="..."> per SCHEMA entry
 * directly under the #params host, so counting those against window.SCHEMA
 * (schema.js's own global) proves the panel never silently drops or
 * duplicates a stage. */
export default async function run(ctx) {
  const page = ctx.page;
  const counts = await page.evaluate(() => ({
    schema: (window.SCHEMA || []).length,
    rendered: document.querySelectorAll("#params > section.stage").length,
    ids: Array.from(document.querySelectorAll("#params > section.stage")).map((el) => el.dataset.stage),
  }));

  if (counts.schema === 0) {
    return { status: "FAIL", evidence: "window.SCHEMA is empty or missing" };
  }
  if (counts.schema !== counts.rendered) {
    return {
      status: "FAIL",
      evidence: "SCHEMA has " + counts.schema + " section(s), #params rendered " + counts.rendered + " (" + counts.ids.join(", ") + ")",
    };
  }
  return { status: "PASS", evidence: counts.rendered + " of " + counts.schema + " SCHEMA sections rendered inside #params (" + counts.ids.join(", ") + ")" };
}
