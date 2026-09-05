/* mobile: the phone layout (contract C8,
 * plan/2026-09-05-studio-projects/PLAN.md), driven at a real phone viewport
 * (390 x 844 at device scale 3, isMobile and hasTouch on, so the page gets
 * Chrome's actual mobile emulation and not just a narrow desktop window).
 *
 * What is under test is studio/static/mobile.js plus the mobile block at the
 * end of style.css plus the data-mobile / data-mobilepage attributes
 * static/sidebars.js paints on <html> before first paint. Everything below is
 * a real read of the real DOM after a real click or a real TOUCH gesture:
 * the slider drag uses page.touchscreen, not page.mouse, because "does this
 * work under a thumb" is the whole question this contract exists to answer,
 * and a mouse drag would pass on the old mouse-event controls too.
 *
 * The claims, in order:
 *   1. At a phone viewport <html> carries data-mobile="on" and #mobilebar is
 *      displayed with exactly the four page buttons: Clips, Preview, Grade,
 *      History.
 *   2. Each of the four opens its page: data-mobilepage moves, the region for
 *      that page is rendered and the other two are not, and (for Grade and
 *      History) data-paramtab follows the button.
 *   3. In both themes, each of those four pages is screenshotted. The theme
 *      claim itself is asserted on getComputedStyle, never on the PNG: this
 *      headless Chrome is known on this machine to hand back a screenshot
 *      still showing the previous theme even when the DOM is already correct
 *      (documented at length in spec 18's own comment and checkpoint). The
 *      PNGs are evidence for a human, exactly as in spec 07 and spec 18, and
 *      a screenshot failure never turns a real PASS into a FAIL.
 *   4. On the Grade page the floating preview is a sticky element pinned to
 *      the top of the pane, it survives the pane being scrolled (its top does
 *      not move while scrollTop does), and it is showing a picture.
 *   5. A slider below the preview is scrolled into view, is genuinely below
 *      the preview's bottom edge, and a real touch drag on it changes the
 *      live config (read through #jsonBtn/#jsonText, the same no-private-JS
 *      way every other spec reads it).
 *   6. That same drag changes the preview's own pixels: a screenshot clipped
 *      to the preview box differs before and after.
 *   7. A reload keeps the chosen page, the way the sidebars' own collapsed
 *      state already does.
 *
 * The page is put back to the desktop viewport, the Grade tab and the theme
 * it was found in before returning, so this spec leaves nothing behind for a
 * later run of the suite.
 */

const PHONE = { width: 390, height: 844, deviceScaleFactor: 3, isMobile: true, hasTouch: true };
const EXPOSURE_TRACK = 'section.stage[data-stage="convert"] .ctl-slider .track';

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;
  const notes = [];

  async function shot(name) {
    try {
      const path = (await import("node:path")).default;
      const { fileURLToPath } = await import("node:url");
      const { mkdirSync } = await import("node:fs");
      const here = path.dirname(fileURLToPath(import.meta.url));
      const dir = path.resolve(here, "..", "..", "shots"); // specs -> tests -> studio -> shots
      mkdirSync(dir, { recursive: true });
      await page.screenshot({ path: path.join(dir, name) });
      return name;
    } catch (err) {
      return null;                       // evidence only, never pass/fail
    }
  }

  async function clickBar(label) {
    const ok = await page.evaluate((l) => {
      const btns = Array.prototype.slice.call(document.querySelectorAll("#mobilebar .mobiletab"));
      const b = btns.filter((x) => x.textContent.trim() === l)[0];
      if (!b) return false;
      b.click();
      return true;
    }, label);
    if (!ok) throw new Error('no #mobilebar button labelled "' + label + '"');
    await sleep(450);
  }

  async function pageState() {
    return page.evaluate(() => {
      const rendered = (id) => {
        const el = document.getElementById(id);
        if (!el) return null;
        const cs = getComputedStyle(el);
        // "on the page" means both drawn and reachable: the two sidebars are
        // display: none when they are not the page, and the middle stays laid
        // out at opacity 0 (see the mobile block's own comment in style.css).
        return cs.display !== "none" && cs.opacity !== "0";
      };
      const mp = document.getElementById("mobilepreview");
      const mpcs = mp ? getComputedStyle(mp) : null;
      const mpr = mp ? mp.getBoundingClientRect() : null;
      return {
        mobile: document.documentElement.getAttribute("data-mobile"),
        page: document.documentElement.getAttribute("data-mobilepage"),
        tab: document.documentElement.getAttribute("data-paramtab"),
        left: rendered("sidebarLeft"),
        main: rendered("mainBg"),
        right: rendered("sidebarRight"),
        preview: mpcs ? {
          display: mpcs.display, position: mpcs.position,
          top: Math.round(mpr.top), height: Math.round(mpr.height)
        } : null,
        barButtons: Array.prototype.map.call(
          document.querySelectorAll("#mobilebar .mobiletab"),
          (b) => b.textContent.trim()
        ),
        barDisplay: getComputedStyle(document.getElementById("mobilebar")).display,
      };
    });
  }

  async function readConfig() {
    await page.click("#jsonBtn");
    await sleep(150);
    const text = await page.$eval("#jsonText", (el) => el.value);
    await page.evaluate(() => {
      const o = document.getElementById("jsonOverlay");
      if (o) o.classList.remove("on");
    });
    return JSON.parse(text);
  }

  const startingTheme = (await page.evaluate(
    () => document.documentElement.getAttribute("data-theme")
  )) || "dark";

  await page.setViewport(PHONE);
  await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await sleep(900);

  // --- 1. mobile mode is on and the bar carries the four pages -------------
  let st = await pageState();
  if (st.mobile !== "on") {
    return fail('at ' + PHONE.width + 'x' + PHONE.height + ' <html data-mobile> reads ' + JSON.stringify(st.mobile) + ', expected "on"');
  }
  if (st.barDisplay === "none") return fail("#mobilebar is display: none at a phone viewport");
  const want = ["Clips", "Preview", "Grade", "History"];
  if (st.barButtons.join(",") !== want.join(",")) {
    return fail("#mobilebar buttons are " + JSON.stringify(st.barButtons) + ", expected " + JSON.stringify(want));
  }
  const layoutWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  if (layoutWidth > PHONE.width + 1) {
    return fail("the page lays out " + layoutWidth + "px wide on a " + PHONE.width + "px viewport, so it is being scaled down rather than laid out for the phone (a missing viewport meta tag does exactly this)");
  }
  notes.push("data-mobile=on, four page buttons, layout width " + layoutWidth + "px");

  // --- 2 and 3. every page, in both themes ---------------------------------
  const PAGES = [
    { label: "Clips", page: "clips", region: "left" },
    { label: "Preview", page: "preview", region: "main" },
    { label: "Grade", page: "params", region: "right", tab: "grade" },
    { label: "History", page: "params", region: "right", tab: "history" },
  ];
  const themeBg = {};
  const savedShots = [];

  for (let pass = 0; pass < 2; pass++) {
    const theme = pass === 0 ? startingTheme : (startingTheme === "light" ? "dark" : "light");
    if (pass === 1) {
      // #themeToggle lives in the right sidebar's utility strip, so it is on
      // screen only while the params page is showing: switch there first.
      await clickBar("Grade");
      await page.click("#themeToggle");
      await sleep(500);
      const now = await page.evaluate(() => document.documentElement.getAttribute("data-theme")) || "dark";
      if (now !== theme) return fail("clicking #themeToggle left data-theme at " + JSON.stringify(now) + ", expected " + theme);
    }
    themeBg[theme] = await page.evaluate(
      () => getComputedStyle(document.getElementById("mobilebar")).backgroundColor
    );

    for (const p of PAGES) {
      await clickBar(p.label);
      st = await pageState();
      if (st.page !== p.page) {
        return fail('the "' + p.label + '" button left data-mobilepage at ' + JSON.stringify(st.page) + ', expected "' + p.page + '"');
      }
      if (p.tab && st.tab !== p.tab) {
        return fail('the "' + p.label + '" button left data-paramtab at ' + JSON.stringify(st.tab) + ', expected "' + p.tab + '"');
      }
      if (!st[p.region]) {
        return fail('on the "' + p.label + '" page its own region (#' + (p.region === "main" ? "mainBg" : p.region === "left" ? "sidebarLeft" : "sidebarRight") + ") is not rendered");
      }
      const others = ["left", "main", "right"].filter((r) => r !== p.region);
      const leaking = others.filter((r) => st[r]);
      if (leaking.length) {
        return fail('on the "' + p.label + '" page, ' + leaking.join(" and ") + " is still rendered: pages are supposed to be one at a time");
      }
      const name = "22-mobile-" + (p.tab || p.page) + "-" + theme + ".png";
      const wrote = await shot(name);
      if (wrote) savedShots.push(wrote);
    }
  }

  if (themeBg.dark === themeBg.light) {
    return fail("#mobilebar's computed background is " + themeBg.dark + " in both themes, so the mobile bar is not following the theme");
  }
  notes.push("all four pages opened in both themes (mobile bar background dark " + themeBg.dark + " vs light " + themeBg.light + "), "
    + savedShots.length + " screenshot(s) saved to studio/shots/");

  // put the theme back before the interactive part, so the run leaves the
  // page as it found it even if an assertion below returns early
  if ((await page.evaluate(() => document.documentElement.getAttribute("data-theme")) || "dark") !== startingTheme) {
    await clickBar("Grade");
    await page.click("#themeToggle");
    await sleep(300);
  }

  // --- 4. the floating preview is pinned -----------------------------------
  await clickBar("Grade");
  st = await pageState();
  if (!st.preview || st.preview.display === "none") {
    return fail("on the Grade page #mobilepreview computes display: " + (st.preview && st.preview.display) + ", expected it to be shown");
  }
  if (st.preview.position !== "sticky") {
    return fail("#mobilepreview computes position: " + st.preview.position + ', expected "sticky" (that is what pins it while the panels scroll under it)');
  }
  if (st.preview.height < 100) {
    return fail("#mobilepreview is only " + st.preview.height + "px tall, expected a real reduced height preview");
  }

  const pinned = await page.evaluate(async () => {
    const pane = document.querySelector('.parampane[data-paramtab="grade"]');
    const mp = document.getElementById("mobilepreview");
    const before = mp.getBoundingClientRect().top;
    pane.scrollTop = 0;
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const atTop = mp.getBoundingClientRect().top;
    pane.scrollTop = 420;
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    return {
      before: Math.round(before),
      atTop: Math.round(atTop),
      scrolled: Math.round(mp.getBoundingClientRect().top),
      scrollTop: Math.round(pane.scrollTop),
      scrollHeight: pane.scrollHeight,
      clientHeight: pane.clientHeight,
    };
  });
  if (pinned.scrollTop < 100) {
    return fail("the Grade pane did not scroll (scrollTop " + pinned.scrollTop + "), so nothing can be proven about the preview staying pinned");
  }
  if (Math.abs(pinned.scrolled - pinned.atTop) > 2) {
    return fail("#mobilepreview moved from top " + pinned.atTop + " to " + pinned.scrolled + " while the pane scrolled to " + pinned.scrollTop + ": it is not pinned");
  }
  notes.push("preview pinned at top " + pinned.scrolled + " while the pane scrolled to " + pinned.scrollTop + " of " + pinned.scrollHeight);

  // --- 5. a real touch drag on a slider below the preview ------------------
  const target = await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    el.scrollIntoView({ block: "center" });
    return true;
  }, EXPOSURE_TRACK);
  if (!target) return fail("no " + EXPOSURE_TRACK + " found in the Grade pane");
  await sleep(350);

  const geom = await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    const mp = document.getElementById("mobilepreview");
    const r = el.getBoundingClientRect();
    const m = mp.getBoundingClientRect();
    return {
      track: { x: r.left + r.width / 2, y: r.top + r.height / 2, top: Math.round(r.top), width: Math.round(r.width) },
      preview: { left: Math.round(m.left), top: Math.round(m.top), w: Math.round(m.width), h: Math.round(m.height), bottom: Math.round(m.bottom) },
    };
  }, EXPOSURE_TRACK);
  if (geom.track.top <= geom.preview.bottom) {
    return fail("after scrolling it into view the exposure slider's top is " + geom.track.top + ", which is not below the preview's bottom edge " + geom.preview.bottom);
  }

  const before = await readConfig();
  const shotClip = {
    x: geom.preview.left, y: geom.preview.top,
    width: geom.preview.w, height: geom.preview.h,
  };
  const pixelsBefore = await page.screenshot({ clip: shotClip, encoding: "base64" });

  await page.touchscreen.touchStart(geom.track.x, geom.track.y);
  await page.touchscreen.touchMove(geom.track.x + 40, geom.track.y);
  await page.touchscreen.touchMove(geom.track.x + 70, geom.track.y);
  await page.touchscreen.touchEnd();
  await sleep(2500);                     // let the render the drag asked for land

  const after = await readConfig();
  if (after.convert.exposure === before.convert.exposure) {
    return fail("a real touch drag on the exposure slider left convert.exposure at " + before.convert.exposure + ": touch input is not reaching the control");
  }
  notes.push("touch drag moved convert.exposure " + before.convert.exposure + " to " + after.convert.exposure
    + " (slider top " + geom.track.top + ", preview bottom " + geom.preview.bottom + ")");

  // --- 6. the preview's own pixels moved with it ---------------------------
  const pixelsAfter = await page.screenshot({ clip: shotClip, encoding: "base64" });
  if (pixelsBefore === pixelsAfter) {
    return fail("the floating preview's pixels are byte identical before and after the exposure change, so it is not tracking the viewer");
  }
  notes.push("preview pixels changed with the grade (" + pixelsBefore.length + " to " + pixelsAfter.length + " base64 chars)");
  await shot("22-mobile-grade-after-drag.png");

  // --- 7. the chosen page survives a reload --------------------------------
  await clickBar("History");
  await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await sleep(700);
  const reloaded = await pageState();
  if (reloaded.page !== "params" || reloaded.tab !== "history") {
    return fail("after a reload data-mobilepage/data-paramtab read " + JSON.stringify(reloaded.page) + "/" + JSON.stringify(reloaded.tab) + ", expected params/history");
  }
  if (reloaded.mobile !== "on") return fail("after a reload data-mobile reads " + JSON.stringify(reloaded.mobile));
  notes.push("reload kept the History page open");

  // --- leave the page as it was found --------------------------------------
  await clickBar("Grade");
  await page.setViewport(ctx.defaultViewport);
  await sleep(300);
  await page.evaluate(() => {
    try { window.localStorage.setItem("fixxr-studio-mobilepage", "preview"); } catch (e) { /* private mode */ }
    document.documentElement.setAttribute("data-mobilepage", "preview");
  });

  return { status: "PASS", evidence: notes.join("; ") };
}
