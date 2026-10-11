// Phone-sized, touch-only checks: the bottom bar, the More sheet, tap-to-preview in the
// library, exact trim buttons, and the sideways servo table.
//   BASE=http://127.0.0.1:8090 node touch.mjs
import { chromium } from "playwright-core";
import fs from "node:fs";

const base = process.env.BASE || "http://127.0.0.1:8090";
fs.mkdirSync("flow", { recursive: true });
const browser = await chromium.launch({ channel: "chrome", args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
const page = await ctx.newPage();
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(`console: ${m.text()}`); });
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("dialog", (d) => d.accept());
const shot = (n) => page.screenshot({ path: `flow/390-${n}.png` });
const go = async (r) => { await page.goto(`${base}/#${r}`); await page.waitForTimeout(1200); };
const step = async (name, fn) => { try { await fn(); console.log("PASS", name); } catch (e) { console.log("FAIL", name, "::", String(e.message).split("\n")[0]); await shot("fail-" + name.replace(/\W+/g, "_")); } };

await page.request.post(base + "/api/dev/fake", { data: { reset: true } });

await step("phone: bottom bar and More sheet reach every page", async () => {
  await go("/library");
  await page.locator(".tabbar").getByRole("link", { name: "Sessions" }).tap();
  await page.waitForURL(/#\/sessions/);
  await page.locator(".tabbar").getByRole("button", { name: "More" }).tap();
  await shot("more-sheet");
  for (const name of ["Library", "Studio", "Record", "Generate", "Live", "Plans", "Sessions", "Coach", "Calibrate"]) {
    await page.locator(".sheet").getByRole("link", { name: new RegExp("^" + name) }).waitFor();
  }
  await page.locator(".sheet").getByRole("link", { name: /^Calibrate/ }).tap();
  await page.waitForURL(/#\/calibrate/);
  await page.getByRole("button", { name: "Stop the robot now" }).waitFor();
});

await step("phone: a tap previews, Play on robot is a separate press, then a confirm", async () => {
  await go("/library");
  const row = page.locator(".move", { hasText: "Wave hello" });
  await row.locator(".move-main").tap();
  await page.locator(".library-preview .screen-title", { hasText: "Wave hello" }).waitFor();
  if (await page.getByRole("alertdialog").count()) throw new Error("a tap opened the play confirm");
  await page.waitForTimeout(700);
  await shot("library-tapped");
  await row.getByRole("button", { name: "Play on robot" }).tap();
  await page.getByRole("alertdialog").getByRole("button", { name: "Move the robot" }).tap();
  await page.locator(".robotbar", { hasText: /Playing|finished/ }).waitFor({ timeout: 6000 });
  await page.getByRole("button", { name: "Stop robot" }).tap();
});

await step("phone: trim to the tenth of a second with buttons", async () => {
  await go("/record");
  await page.getByRole("button", { name: /Start recording|Record a new take/ }).tap();
  await page.waitForTimeout(3200);
  await page.getByRole("button", { name: "Stop recording" }).tap();
  await page.getByRole("heading", { name: "Trim" }).waitFor({ timeout: 6000 });
  for (let i = 0; i < 3; i++) await page.getByRole("button", { name: "Start 0.1 seconds later" }).tap();
  await page.getByRole("button", { name: "End 0.1 seconds earlier" }).tap();
  const v = await page.getByLabel("Start time in seconds").inputValue();
  if (v !== "0.3") throw new Error("start is " + v);
  await page.getByLabel("End time in seconds").scrollIntoViewIfNeeded();
  await shot("record-trim");
  await page.getByRole("button", { name: "Discard this take" }).tap();
});

await step("phone: the servo table says it scrolls and keeps the servo name in view", async () => {
  await go("/calibrate");
  await page.locator(".servo-table tbody tr").first().waitFor({ timeout: 6000 });
  await page.locator(".scroll-hint").first().waitFor();
  const scroller = page.locator(".servo-scroll").first();
  await scroller.evaluate((el) => { el.scrollLeft = 400; });
  await page.waitForTimeout(300);
  const left = await scroller.locator("tbody th").first().evaluate((el) => el.getBoundingClientRect().left - el.closest(".servo-scroll").getBoundingClientRect().left);
  if (Math.abs(left) > 3) throw new Error("servo name scrolled away: " + left);
  await scroller.scrollIntoViewIfNeeded();
  await shot("calibrate-scrolled");
});

await step("phone: replay names the set", async () => {
  const id = (await (await page.request.get(base + "/api/sessions")).json()).find((r) => r.kind === "exercise" && r.status === "completed" && r.sets >= 2).id;
  await go("/sessions/" + id);
  await page.getByRole("button", { name: /Replay on the twin/ }).tap();
  await page.locator(".replay-what", { hasText: /set 1/ }).waitFor();
  await page.locator(".set-picker .chip").nth(1).tap();
  await page.getByRole("button", { name: /Replay on the twin/ }).tap();
  await page.locator(".replay-what", { hasText: /set 2/ }).waitFor();
  await page.locator(".replay-what").scrollIntoViewIfNeeded();
  await shot("replay-set");
});

console.log(errors.length ? errors.join("\n") : "no console errors");
await browser.close();
