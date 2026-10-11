// node shots.mjs <width> <route> [route...]   -> out/<width>-<route>.png, prints console errors
import { chromium } from "playwright-core";
import fs from "node:fs";

const [width, ...routes] = process.argv.slice(2);
const base = process.env.BASE || "http://127.0.0.1:8090";
fs.mkdirSync("out", { recursive: true });
const browser = await chromium.launch({ channel: "chrome", args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
const ctx = await browser.newContext({ viewport: { width: +width, height: +width < 600 ? 844 : 900 }, deviceScaleFactor: 1, colorScheme: process.env.SCHEME || "light" });
const page = await ctx.newPage();
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(`[${page.url().split("#")[1]}] ${m.text()}`); });
page.on("pageerror", (e) => errors.push(`[${page.url().split("#")[1]}] PAGEERROR ${e.message}`));
for (const r0 of routes) {
  const route = r0 === "home" ? "/" : "/" + r0;
  await page.goto(`${base}/#${route}`);
  await page.reload();
  await page.waitForTimeout(+(process.env.WAIT || 2500));
  const name = route.replace(/\//g, "_") || "_";
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (overflow > 1) errors.push(`[${route}] horizontal overflow ${overflow}px at ${width}`);
  await page.screenshot({ path: `out/${width}${name}.png`, fullPage: process.env.FULL !== "0" });
}
console.log(errors.length ? errors.join("\n") : "no errors");
await browser.close();
