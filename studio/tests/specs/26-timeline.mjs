/* timeline: the rebuilt ruler (static/timeline.js).
 *
 * Founder's words: "improve the timeline, scrub, feel, ux of the timeline?
 * like it should be wayyyy better than what it is. Clean."
 *
 * The claims, in order:
 *   1. Pointer scrub is direct: a real mouse down on the ruler puts the
 *      playhead where it was pressed, a real drag follows the pointer while
 *      it is held (checked mid drag, not only at the end), the playhead
 *      element and #timeLabel and S.time all agree, and the landing time is
 *      the pressed position to within a frame.
 *   2. Keyboard stepping is frame accurate: "." and "," move exactly one
 *      frame, shift moves exactly ten, home and end land on the first and
 *      last frame, and the arrow keys do the same with the ruler focused.
 *   3. Marks are flags on the ruler at the time they mark: M adds one, its
 *      flag sits at that fraction of the track, clicking it jumps there, and
 *      its own delete control removes exactly that one.
 *   4. The loop range is a drag, not a text box: dragging in the range lane
 *      sets the play length, it round trips through extras.play_secs on the
 *      project exactly as the old "secs" field did, and it comes back on a
 *      reload as a band on the ruler.
 *   5. The ruler is a real scale: ticks and labels adapt to the width, they
 *      stay inside the track, and they read in order.
 *   6. Mobile mode (contract C8) still lays the timeline out: nothing spills
 *      out of the card, and every lane and control is a touch target rather
 *      than a mouse target.
 *
 * SKIPs rather than fails when the input probe says this headless Chrome
 * does not deliver the events a drag is made of, matching 05-sidebar-drag.
 */

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function fail(evidence) { return { status: "FAIL", evidence: evidence }; }

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;
  const notes = [];
  const problems = [];

  if (!ctx.firstClip) {
    return { status: "SKIP", evidence: "no clips from /api/clips, so there is no duration to scrub" };
  }
  const need = ["mousedown", "mouseup", "mousemove"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(", ") + " never reach the page, so a real scrub cannot be driven",
    };
  }

  /* Which clip the app has open, read off the app's own source readout
   * (#clipInfo's first row is kv("file", entry.name) in drawClipInfo), the
   * same way 21-match-pick does it: an earlier spec can leave a different
   * clip selected, and both the duration this spec measures against and the
   * project extras it reads back are per clip. */
  const shownClip = await page.evaluate(() => {
    const n = document.querySelector("#clipInfo div span");
    return n ? n.textContent.trim() : "";
  });
  const clipName = shownClip || ctx.firstClip;

  const clipsRes = await fetch(base + "/api/clips").then((r) => r.json());
  const entry = (clipsRes.clips || []).find((c) => c.name === clipName);
  const duration = (entry && entry.duration) || 0;
  const fps = (entry && entry.fps) || 24;
  const frame = 1 / fps;
  if (!(duration > 1)) {
    return { status: "SKIP", evidence: "clip " + clipName + " is " + duration + "s, too short to scrub across" };
  }

  const consoleFence = ctx.consoleEvents.length;
  const errorFence = ctx.pageErrors.length;

  /* ---- helpers ----------------------------------------------------------- */

  // The exact playhead, not the two-decimal label: a frame is 0.0417s on this
  // footage and "one frame accurate" cannot be measured through a rounded
  // string. StudioTimeline.time() is a read only view of app.js's own S.time.
  const readTime = () => page.evaluate(() => window.StudioTimeline.time());
  const readLabel = () => page.evaluate(() =>
    parseFloat(document.getElementById("timeLabel").textContent) || 0);

  // Where the ruler is on screen right now. The timeline card lives at the
  // bottom of a scrolling column, so this scrolls it into view first and then
  // measures; timeline.js invalidates its own cached rect off the same scroll.
  async function trackBox() {
    return page.evaluate(() => {
      const card = document.querySelector('[gs-id="timeline"]');
      if (card) card.scrollIntoView({ block: "end" });
      const t = document.getElementById("scrub").getBoundingClientRect();
      const r = document.getElementById("tlRange").getBoundingClientRect();
      return {
        x: t.left, y: t.top, w: t.width, h: t.height,
        midY: t.top + 26,                      // the filmstrip lane, over the ticks
        rangeY: r.top + r.height / 2,
      };
    });
  }

  // The playhead's own rendered position, read off the transform timeline.js
  // writes, so "the picture of the playhead" and "the number" are checked
  // separately rather than assumed to be the same thing.
  const headX = () => page.evaluate(() => {
    const el = document.getElementById("tlHead");
    const m = new DOMMatrixReadOnly(getComputedStyle(el).transform);
    return m.m41;
  });

  async function scrubDrag(box, fromFrac, toFrac, steps) {
    await page.mouse.move(box.x + box.w * fromFrac, box.midY);
    await page.mouse.down();
    await sleep(60);
    const mid = [];
    const n = steps || 6;
    for (let i = 1; i <= n; i++) {
      const f = fromFrac + ((toFrac - fromFrac) * i) / n;
      await page.mouse.move(box.x + box.w * f, box.midY);
      await sleep(40);
      if (i === Math.round(n / 2)) mid.push({ f: f, t: await readTime() });
    }
    await page.mouse.up();
    await sleep(250);
    return mid[0];
  }

  try {
    /* ---- 1. pointer scrub is direct ------------------------------------- */
    let box = await trackBox();
    if (!(box.w > 200)) return fail("the ruler is only " + box.w.toFixed(0) + "px wide, nothing can be measured on it");

    const mid = await scrubDrag(box, 0.2, 0.7);
    const landed = await readTime();
    const label = await readLabel();
    const px = await headX();
    const wantLanded = 0.7 * duration;
    const wantMid = mid.f * duration;

    if (Math.abs(mid.t - wantMid) > 2 * frame) {
      problems.push("mid drag the playhead read " + mid.t.toFixed(3) + "s where the pointer was over "
        + wantMid.toFixed(3) + "s, so the picture is not following the pointer during the drag");
    }
    if (Math.abs(landed - wantLanded) > 2 * frame) {
      problems.push("the drag ended over " + wantLanded.toFixed(3) + "s but the playhead is at "
        + landed.toFixed(3) + "s");
    }
    if (Math.abs(label - landed) > 0.01) {
      problems.push("#timeLabel reads " + label + "s while the playhead is at " + landed.toFixed(3) + "s");
    }
    if (Math.abs(px - (landed / duration) * box.w) > 3) {
      problems.push("the playhead is drawn at " + px.toFixed(1) + "px but "
        + landed.toFixed(3) + "s of " + duration.toFixed(2) + "s is "
        + ((landed / duration) * box.w).toFixed(1) + "px along a " + box.w.toFixed(0) + "px ruler");
    }
    if (!problems.length) {
      notes.push("scrub: pressed at 20%, dragged to 70% of a " + box.w.toFixed(0)
        + "px ruler, followed mid drag at " + mid.t.toFixed(2) + "s, landed at "
        + landed.toFixed(3) + "s (wanted " + wantLanded.toFixed(3) + "s), playhead drawn at "
        + px.toFixed(1) + "px");
    }

    /* ---- 2. keyboard stepping is frame accurate ------------------------- */
    if (!problems.length) {
      const steps = [];
      const t0 = await readTime();
      await page.keyboard.press("Period");
      await sleep(120);
      const t1 = await readTime();
      steps.push(["one frame forward (.)", t1 - t0, frame]);

      await page.keyboard.down("Shift");
      await page.keyboard.press("Period");
      await page.keyboard.up("Shift");
      await sleep(120);
      const t2 = await readTime();
      steps.push(["ten frames forward (shift .)", t2 - t1, 10 * frame]);

      await page.keyboard.press("Comma");
      await sleep(120);
      const t3 = await readTime();
      steps.push(["one frame back (,)", t3 - t2, -frame]);

      for (const [what, got, want] of steps) {
        if (Math.abs(got - want) > frame * 0.05) {
          problems.push(what + " moved " + got.toFixed(4) + "s, one frame is " + frame.toFixed(4) + "s");
        }
      }

      await page.keyboard.press("Home");
      await sleep(150);
      const home = await readTime();
      await page.keyboard.press("End");
      await sleep(150);
      const end = await readTime();
      if (home !== 0) problems.push("home left the playhead at " + home + "s, not 0");
      if (Math.abs(end - (duration - frame)) > frame * 0.05) {
        problems.push("end left the playhead at " + end.toFixed(3) + "s, the last frame is "
          + (duration - frame).toFixed(3) + "s");
      }

      // The ruler takes focus from the scrub above, so the arrow keys are its
      // own handler rather than the global one: same step, checked separately.
      await page.keyboard.press("ArrowLeft");
      await sleep(120);
      const arrowed = await readTime();
      if (Math.abs(arrowed - (end - frame)) > frame * 0.05) {
        problems.push("ArrowLeft on the focused ruler moved " + (arrowed - end).toFixed(4)
          + "s, one frame is " + frame.toFixed(4) + "s");
      }
      if (!problems.length) {
        notes.push("stepping: . = 1 frame, shift . = 10, , = 1 back, arrows = 1, all within 5% of "
          + frame.toFixed(4) + "s; home 0s, end " + end.toFixed(3) + "s");
      }
    }

    /* ---- 3. marks are flags on the ruler --------------------------------- */
    if (!problems.length) {
      await page.evaluate(() => {
        // Start from no marks however many an earlier spec or an earlier run
        // of this one left behind; the button hides itself when there are none.
        const btn = document.getElementById("clearMarksBtn");
        if (btn) btn.click();
      });
      await sleep(150);

      box = await trackBox();
      await scrubDrag(box, 0.5, 0.25, 3);
      const markA = await readTime();
      await page.keyboard.press("KeyM");
      await sleep(150);
      await scrubDrag(box, 0.25, 0.8, 3);
      const markB = await readTime();
      await page.keyboard.press("KeyM");
      await sleep(200);

      const flags = await page.evaluate(() => Array.from(document.querySelectorAll("#marks .tlmark"))
        .map((el) => ({ t: Number(el.dataset.t), left: parseFloat(el.style.left) })));
      if (flags.length !== 2) {
        problems.push("two marks were made but the ruler shows " + flags.length + " flag(s)");
      } else {
        const wantA = (markA / duration) * 100;
        const off = Math.abs(flags[0].left - wantA);
        if (off > 0.4) {
          problems.push("the first mark is at " + markA.toFixed(2) + "s (" + wantA.toFixed(2)
            + "% of the clip) but its flag is drawn at " + flags[0].left.toFixed(2) + "%");
        }
        // Click the second flag: the playhead is at the second mark already,
        // so jump away first and prove the click brought it back.
        await scrubDrag(box, 0.8, 0.1, 3);
        const flagBox = await page.evaluate(() => {
          const el = document.querySelectorAll("#marks .tlmark")[1].querySelector(".tlmarkflag");
          const r = el.getBoundingClientRect();
          return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        });
        await page.mouse.click(flagBox.x, flagBox.y);
        await sleep(300);
        const jumped = await readTime();
        if (Math.abs(jumped - markB) > 0.02) {
          problems.push("clicking the second flag landed at " + jumped.toFixed(3)
            + "s, the mark it stands for is at " + markB.toFixed(3) + "s");
        }

        // ... and its own delete control removes exactly that one.
        const killBox = await page.evaluate(() => {
          const el = document.querySelectorAll("#marks .tlmark")[1].querySelector(".tlmarkkill");
          const r = el.getBoundingClientRect();
          return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        });
        await page.mouse.move(killBox.x, killBox.y);
        await sleep(250);                       // the kill fades in on hover
        await page.mouse.click(killBox.x, killBox.y);
        await sleep(250);
        const left = await page.evaluate(() => Array.from(document.querySelectorAll("#marks .tlmark"))
          .map((el) => Number(el.dataset.t)));
        if (left.length !== 1 || Math.abs(left[0] - markA) > 0.02) {
          problems.push("after deleting the second mark the ruler shows " + JSON.stringify(left)
            + ", expected just the first at " + markA.toFixed(2) + "s");
        } else {
          notes.push("marks: two flags at " + markA.toFixed(2) + "s and " + markB.toFixed(2)
            + "s, the second drawn within " + off.toFixed(2) + "% of its time, click jumped to it, "
            + "its own x removed only it");
        }
      }
      await page.evaluate(() => document.getElementById("clearMarksBtn").click());
      await sleep(150);
    }

    /* ---- 4. the loop range is a drag and it persists --------------------- */
    if (!problems.length) {
      /* A project row exists only once something has opened it, and on a cold
         data dir the app is still opening this clip's project while the page
         is already usable. saveProjectExtra does cope (it opens and retries),
         but the write and that open would then race, so this waits for the
         row rather than starting the drag into it. */
      for (let i = 0; i < 25; i++) {
        const state = await fetch(base + "/api/project?clip=" + encodeURIComponent(clipName))
          .then((r) => r.json()).catch(() => ({}));
        if (state && state.open) break;
        await sleep(200);
      }
      box = await trackBox();
      const inFrac = 0.1;
      const outFrac = inFrac + 2 / duration;      // a two second range
      await page.mouse.move(box.x + box.w * inFrac, box.rangeY);
      await page.mouse.down();
      await sleep(60);
      for (let i = 1; i <= 5; i++) {
        await page.mouse.move(box.x + box.w * (inFrac + ((outFrac - inFrac) * i) / 5), box.rangeY);
        await sleep(40);
      }
      await page.mouse.up();
      await sleep(500);                            // past the extra save

      const field = await page.$eval("#playDur", (el) => el.value);
      const secs = parseFloat(field);
      if (!(Math.abs(secs - 2) < 0.15)) {
        problems.push("a two second drag in the range lane left #playDur reading "
          + JSON.stringify(field) + ", expected about 2");
      }
      const loopFields = await page.evaluate(() => [
        document.getElementById("loopStart").value, document.getElementById("loopEnd").value]);
      const inTime = await readTime();
      if (Math.abs(parseFloat(loopFields[0]) - inTime) > 0.02
        || Math.abs(parseFloat(loopFields[1]) - (inTime + secs)) > 0.05) {
        problems.push("the GPU loop fields read " + JSON.stringify(loopFields)
          + " but the range on the ruler is " + inTime.toFixed(2) + "s + " + secs.toFixed(2) + "s");
      }
      // saveProjectExtra opens the project first when the account has none
      // open yet and retries the write, so this polls for the value instead
      // of trusting one sleep to cover both round trips.
      let saved;
      let bag = {};
      for (let i = 0; i < 20; i++) {
        const proj = await fetch(base + "/api/project?clip=" + encodeURIComponent(clipName))
          .then((r) => r.json()).catch(() => ({}));
        bag = proj.extras || {};
        saved = bag.play_secs;
        if (typeof saved === "number" && Math.abs(saved - secs) < 0.01) break;
        await sleep(200);
      }
      if (!(typeof saved === "number" && Math.abs(saved - secs) < 0.01)) {
        problems.push("after the drag GET /api/project?clip=" + clipName + " extras.play_secs reads "
          + JSON.stringify(saved) + ", expected " + secs + " (#playDur " + JSON.stringify(field)
          + ", extras " + JSON.stringify(bag) + ")");
      }

      if (!problems.length) {
        await page.reload({ waitUntil: "domcontentloaded" });
        await ctx.waitForBootComplete(20000);
        await sleep(1200);
        const after = await page.evaluate(() => ({
          field: document.getElementById("playDur").value,
          width: document.getElementById("tlBand").style.width,
          all: document.getElementById("tlRange").classList.contains("all"),
        }));
        const bandPct = parseFloat(after.width);
        const wantPct = (secs / duration) * 100;
        if (parseFloat(after.field) !== secs) {
          problems.push("after a reload #playDur reads " + JSON.stringify(after.field)
            + ", expected " + secs);
        } else if (after.all || Math.abs(bandPct - wantPct) > 0.6) {
          problems.push("after a reload the range band is " + after.width + " of the ruler ("
            + (after.all ? "and reads as the whole clip" : "no range") + "), expected about "
            + wantPct.toFixed(2) + "%");
        } else {
          notes.push("range: a drag in the lane set " + secs.toFixed(2)
            + "s, saved as extras.play_secs, and came back after a reload as a band "
            + bandPct.toFixed(2) + "% wide");
        }
      }
      // Back to the documented default so the run leaves the project as the
      // rest of the suite expects to find it.
      await page.evaluate(() => {
        const el = document.getElementById("playDur");
        el.value = "";
        el.dispatchEvent(new Event("change", { bubbles: true }));
        if (window.StudioTimeline) window.StudioTimeline.clipChanged();
      });
      await sleep(300);
    }

    /* ---- 5. the ruler is a real scale ------------------------------------ */
    if (!problems.length) {
      const readRuler = () => page.evaluate(() => {
        const t = document.getElementById("scrub").getBoundingClientRect();
        const labels = Array.from(document.querySelectorAll(".tllabel"));
        return {
          w: t.width,
          count: labels.length,
          ticks: document.querySelectorAll(".tltick").length,
          texts: labels.map((e) => e.textContent),
          lefts: labels.map((e) => parseFloat(e.style.left)),
        };
      });
      const wide = await readRuler();
      // The ruler rebuilds its ticks off a ResizeObserver on its own box, so
      // this waits for the layout to settle rather than for a request.
      await page.setViewport({ width: 900, height: 900 });
      await sleep(600);
      const narrow = await readRuler();
      await page.setViewport(ctx.defaultViewport);
      await sleep(600);

      const ordered = wide.lefts.every((v, i) => i === 0 || v > wide.lefts[i - 1]);
      const inside = wide.lefts.every((v) => v >= 0 && v <= 100);
      if (!wide.count || !ordered || !inside) {
        problems.push("the ruler drew " + wide.count + " labels " + JSON.stringify(wide.texts)
          + " at " + JSON.stringify(wide.lefts) + " (ordered=" + ordered + ", inside=" + inside + ")");
      } else if (narrow.count > wide.count) {
        problems.push("narrowing the window from " + wide.w.toFixed(0) + "px to " + narrow.w.toFixed(0)
          + "px raised the label count from " + wide.count + " to " + narrow.count
          + ", so the tick step is not adapting to the width");
      } else {
        notes.push("ruler: " + wide.count + " labels and " + wide.ticks + " ticks at "
          + wide.w.toFixed(0) + "px (" + wide.texts.slice(0, 4).join(" ") + " ...), "
          + narrow.count + " labels at " + narrow.w.toFixed(0) + "px");
      }
    }

    /* ---- 6. mobile mode still lays the timeline out ---------------------- */
    if (!problems.length) {
      await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
      await sleep(700);
      const phone = await page.evaluate(() => {
        const card = document.querySelector('[gs-id="timeline"]');
        card.scrollIntoView({ block: "end" });
        const cr = card.getBoundingClientRect();
        const track = document.getElementById("scrub").getBoundingClientRect();
        const range = document.getElementById("tlRange").getBoundingClientRect();
        const spill = Array.from(card.querySelectorAll("button, #scrub, #tlRange"))
          .map((el) => el.getBoundingClientRect())
          .filter((r) => r.width > 0 && (r.left < cr.left - 0.5 || r.right > cr.right + 0.5)).length;
        return {
          mobile: !!(window.StudioMobile && window.StudioMobile.isMobile()),
          cardW: cr.width, trackW: track.width, trackH: track.height, rangeH: range.height,
          spill: spill,
          docW: document.documentElement.scrollWidth,
          viewW: window.innerWidth,
        };
      });
      await page.setViewport(ctx.defaultViewport);
      await sleep(500);

      if (!phone.mobile) {
        problems.push("a 390px viewport did not put the page in mobile mode, so this proves nothing");
      } else if (phone.spill) {
        problems.push(phone.spill + " timeline control(s) render outside the card at 390px");
      } else if (phone.docW > phone.viewW + 1) {
        problems.push("the page scrolls sideways at 390px (" + phone.docW + " > " + phone.viewW + ")");
      } else if (!(phone.trackH >= 100 && phone.rangeH >= 24)) {
        problems.push("the phone ruler is " + phone.trackH + "px tall with a " + phone.rangeH
          + "px range lane, which is a mouse target, not a thumb one");
      } else {
        notes.push("mobile: ruler " + phone.trackW.toFixed(0) + "x" + phone.trackH.toFixed(0)
          + " inside a " + phone.cardW.toFixed(0) + "px card, range lane " + phone.rangeH
          + "px, nothing spilling, no sideways scroll");
      }
    }
  } catch (err) {
    return fail("threw: " + (err && err.message ? err.message : String(err)));
  }

  const noisy = ctx.consoleEvents.slice(consoleFence).filter((e) => e.type === "error");
  const threw = ctx.pageErrors.slice(errorFence);
  if (noisy.length || threw.length) {
    problems.push("the timeline logged " + noisy.length + " console error(s) and " + threw.length
      + " page error(s) while being driven: "
      + JSON.stringify(noisy.concat(threw).slice(0, 3)));
  }

  if (problems.length) return fail(problems.join("; "));
  return { status: "PASS", evidence: notes.join("; ") };
}
