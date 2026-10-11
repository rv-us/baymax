// Click through the real flows in dry run and report what happened.
import { chromium } from "playwright-core";
import fs from "node:fs";

const base = process.env.BASE || "http://127.0.0.1:8090";
const width = +(process.env.W || 1280);
fs.mkdirSync("flow", { recursive: true });
const browser = await chromium.launch({ channel: "chrome", args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
const ctx = await browser.newContext({ viewport: { width, height: 900 } });
const page = await ctx.newPage();
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(`console: ${m.text()}`); });
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("dialog", (d) => d.accept());
const log = (...a) => console.log(...a);
const shot = (n) => page.screenshot({ path: `flow/${width}-${n}.png` });
const go = async (r) => { await page.goto(`${base}/#${r}`); await page.waitForTimeout(1200); };
const step = async (name, fn) => { try { await fn(); log("PASS", name); } catch (e) { log("FAIL", name, "::", String(e.message).split("\n")[0]); await shot("fail-" + name.replace(/\W+/g, "_")); } };

await page.request.post(base + "/api/dev/fake", { data: { reset: true } });
await page.goto(base + "/");
await page.waitForTimeout(1500);

await step("landing CTA opens the library", async () => {
  await page.getByRole("link", { name: "Open the dashboard" }).first().click();
  await page.waitForURL(/#\/library/);
  await page.getByRole("heading", { name: "Library", level: 1 }).waitFor();
});

await step("library: hover previews, click asks, confirm plays, status shows", async () => {
  const row = page.locator(".move-main", { hasText: "Wave hello" });
  await row.hover();
  await page.locator(".library-preview .screen-title", { hasText: "Wave hello" }).waitFor();
  await page.waitForTimeout(900);
  await shot("library-hover");
  await row.click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Move the robot" }).click();
  await page.locator(".robotbar", { hasText: /Playing “Wave hello”/ }).waitFor({ timeout: 4000 });
  await shot("library-playing");
  await page.locator(".robotbar", { hasText: /finished on the robot/ }).waitFor({ timeout: 15000 });
  await page.locator(".move-status", { hasText: "Played on the robot" }).waitFor();
});

await step("library: one-click play, then stop", async () => {
  await page.getByRole("switch").click();
  await page.locator(".move-main", { hasText: "Goalpost stretch" }).click();
  await page.locator(".robotbar", { hasText: /Playing “Goalpost stretch”/ }).waitFor({ timeout: 4000 });
  await page.getByRole("button", { name: "Stop robot" }).click();
  await page.locator(".robotbar", { hasText: /stopped part way/ }).waitFor({ timeout: 6000 });
  await page.getByRole("switch").click();
});

await step("library: built-in move plays", async () => {
  await page.getByRole("button", { name: /Built into the robot/ }).click();
  await page.locator(".move-main", { hasText: "Bow" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Move the robot" }).click();
  await page.locator(".robotbar", { hasText: /“Bow” finished/ }).waitFor({ timeout: 8000 });
});

await step("library: a refusal is shown in the robot's words", async () => {
  await page.request.post(base + "/api/dev/fake", { data: { temps: { left_elbow: 90 } } });
  await page.waitForTimeout(600);
  await page.locator(".robotbar", { hasText: /can't play right now/ }).waitFor({ timeout: 6000 });
  await shot("library-blocked");
  await page.request.post(base + "/api/dev/fake", { data: { reset: true } });
  await page.locator(".robotbar", { hasText: /robot is ready|finished/ }).waitFor({ timeout: 8000 });
});

await step("record: record, stop, trim, name, save, play", async () => {
  await go("/record");
  await page.getByRole("button", { name: /Start recording|Record a new take/ }).click();
  await page.getByText("Recording…").waitFor();
  await page.waitForTimeout(4200);
  await shot("record-live");
  await page.getByRole("button", { name: "Stop recording" }).click();
  await page.getByRole("heading", { name: "Trim" }).waitFor({ timeout: 6000 });
  for (let i = 0; i < 6; i++) await page.getByRole("button", { name: "Start 0.1 seconds later" }).click();
  await page.waitForTimeout(1500);
  await page.getByLabel("Name").fill("Flow test wave");
  await shot("record-trim");
  await page.getByRole("button", { name: "Save to the library" }).click();
  await page.getByText(/is in the library, and this recording is logged/).waitFor({ timeout: 6000 });
  await page.getByRole("button", { name: "Play on robot" }).click();
  await page.getByRole("button", { name: "Move the robot" }).click();
  await page.locator(".robotbar", { hasText: /Playing “Flow test wave”|finished/ }).waitFor({ timeout: 6000 });
  await shot("record-saved");
  await page.locator(".robotbar", { hasText: /finished on the robot/ }).waitFor({ timeout: 20000 });
});

await step("sessions: the recording is listed, opens, replays", async () => {
  await go("/sessions");
  await page.locator(".session-row", { hasText: "Recording: Flow test wave" }).first().waitFor();
  await page.locator(".session-row", { hasText: "Recording: Flow test wave" }).first().locator(".session-link").click();
  await page.getByRole("heading", { name: "Recording: Flow test wave" }).waitFor();
  await page.getByRole("button", { name: "Replay" }).click();
  await page.waitForTimeout(1200);
  await shot("recording-session");
});

await step("generate: prompt, preview, save, appears in library", async () => {
  await go("/generate");
  await page.getByLabel("What should the robot do?").fill("clap three times, quickly");
  await page.getByRole("button", { name: "Generate", exact: true }).click();
  await page.getByRole("heading", { name: "Keep it?" }).waitFor({ timeout: 8000 });
  await page.getByText("Made by the mock generator").waitFor();
  await page.waitForTimeout(1200);
  await shot("generate-result");
  await page.getByLabel("Name").fill("Flow test clap");
  await page.getByRole("button", { name: "Save to the library" }).click();
  await page.getByRole("heading", { name: "Saved to the library" }).waitFor();
  await go("/library");
  await page.locator(".move-main", { hasText: "Flow test clap" }).waitFor();
});

await step("generate: discard clears the draft", async () => {
  await go("/generate");
  await page.getByRole("button", { name: "Make another" }).click();
  await page.getByRole("button", { name: "A big cheer" }).click();
  await page.getByRole("heading", { name: "Keep it?" }).waitFor({ timeout: 8000 });
  await page.getByRole("button", { name: "Discard" }).click();
  await page.getByText("The generated motion will play here.").waitFor();
});

await step("sessions: open one, headline, set switch, replay, drill-down", async () => {
  await go("/sessions");
  await page.getByRole("button", { name: /Exercise sessions/ }).click();
  await page.locator(".session-row .session-link").first().waitFor();
  await go("/sessions/" + (await (await page.request.get(base + "/api/sessions")).json()).find((r) => r.kind === "exercise" && r.status === "completed" && r.sets >= 2).id);
  await page.locator(".headline-number b").waitFor();
  await page.getByRole("heading", { name: /Joint angle over time/ }).waitFor();
  await page.getByRole("button", { name: /Replay on the twin/ }).click();
  await page.getByRole("button", { name: "Replay", exact: true }).click();
  await page.waitForTimeout(1500);
  await page.locator(".playhead").waitFor({ state: "attached" });
  await shot("session-replay");
  await page.locator(".set-picker .chip").nth(1).click();
  await page.locator(".parts a").first().click();
  for (const h of ["Range of motion per joint", "Reps: how many, and how good", "Tempo", "Smoothness", "Left against right", "Where each rep drifted", "Session over session"]) {
    await page.getByRole("heading", { name: h }).waitFor();
  }
  if ((await page.locator(".means").count()) < 7) throw new Error("a chart is missing its what-this-means line");
});

await step("coach: ask, streamed answer with the score", async () => {
  await page.getByRole("link", { name: "Ask the coach about this" }).click();
  await page.waitForURL(/#\/coach\//);
  await page.getByRole("button", { name: "How did this session go overall?" }).click();
  await page.locator(".bubble-assistant .bubble-source").waitFor({ timeout: 15000 });
  const text = await page.locator(".bubble-assistant .bubble-text").innerText();
  if (!/out of 100/.test(text)) throw new Error("answer lacks the score: " + text.slice(0, 80));
  await page.getByLabel("Message to the coach").fill("What should we work on next time?");
  await page.keyboard.press("Enter");
  await page.locator(".bubble-assistant .bubble-source").nth(1).waitFor({ timeout: 15000 });
  await shot("coach");
});

await step("calibrate: readout, save a profile, enforce, restore", async () => {
  await go("/calibrate");
  await page.locator(".servo-table tbody tr").first().waitFor({ timeout: 6000 });
  await page.getByLabel("left shoulder pitch minimum").fill("450");
  await page.getByLabel("left shoulder pitch minimum").blur();
  await page.getByRole("button", { name: "Save as a new profile" }).click();
  await page.getByLabel("Name for the new profile").fill("Flow test profile");
  await page.getByRole("button", { name: "Save profile" }).click();
  await page.getByRole("button", { name: "Put this profile in force" }).click();
  await page.getByText("In force on every command").waitFor();
  const r = await (await page.request.post(base + "/api/robot/servo/set", { data: { servo: "left_shoulder_pitch", pulse: 100 } })).json();
  if (r.calibration?.clamped?.left_shoulder_pitch?.applied !== 450) throw new Error("limit not enforced: " + JSON.stringify(r));
  await page.waitForTimeout(1200);
  await shot("calibrate");
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByText("Factory limits (in force)").waitFor({ state: "attached", timeout: 6000 });
});

await step("sessions: delete one from the list", async () => {
  await go("/sessions");
  const before = await page.locator(".session-row .session-link").count();
  await page.locator(".session-row", { hasText: "Recording: Flow test wave" }).first().locator(".row-action").click();
  await page.waitForTimeout(1200);
  const after = await page.locator(".session-row .session-link").count();
  if (after !== before - 1) throw new Error(`rows ${before} -> ${after}`);
});

await step("studio: edit a library animation, live runs a session", async () => {
  await go("/library");
  await page.locator(".move-main", { hasText: "Flow test clap" }).click();
  await page.getByRole("button", { name: "Edit in Studio" }).click();
  await page.waitForURL(/#\/studio/);
  await page.waitForTimeout(1200);
  const v = await page.getByLabel("Animation name").inputValue();
  if (v !== "Flow test clap") throw new Error("studio opened " + v);
  await go("/live");
  await shot("live");
});

await step("stop is on every dashboard page", async () => {
  for (const r of ["/library", "/studio", "/record", "/generate", "/live", "/plans", "/sessions", "/coach", "/calibrate"]) {
    await go(r);
    await page.getByRole("button", { name: "Stop the robot now" }).waitFor({ timeout: 3000 });
  }
});

// tidy what the flows created
for (const id of ["flow_test_wave", "flow_test_clap"]) await page.request.delete(`${base}/api/animations/${id}`);
log(errors.length ? errors.join("\n") : "no console errors");
await browser.close();
