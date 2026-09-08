import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { createServer } from "node:net";
import { join, resolve } from "node:path";
import { chromium } from "@playwright/test";

const root = resolve(import.meta.dirname, "../..");
const temporary = await mkdtemp(join(tmpdir(), "codex-timeline-layout-"));
const project = join(temporary, "layout-fixture");
const sessions = join(temporary, ".codex", "sessions");
const desktop = process.argv.includes("--desktop");
await Promise.all([mkdir(project), mkdir(sessions, { recursive: true }), mkdir(join(temporary, "CodexSessionMonitor"))]);
await writeFile(join(temporary, "CodexSessionMonitor", "config.json"), JSON.stringify({ projects: [project] }));
for (const sessionId of ["layout-completed", "layout-error"]) {
  const rows = [{ timestamp: "2026-07-12T00:00:00Z", type: "session_meta", payload: { session_id: sessionId, cwd: project } }];
  for (let index = 0; index < 25; index += 1) {
    const timestamp = new Date(Date.UTC(2026, 6, 12, 0, index)).toISOString();
    const event = (type, payload) => rows.push({ timestamp, type, payload });
    event("event_msg", { type: "task_started", turn_id: `turn-${index}`, started_at: Date.parse(timestamp) / 1000 });
    event("event_msg", { type: "user_message", message: `Layout message ${index}\n${"Long message content. ".repeat(12)}` });
    event("response_item", { type: "function_call", name: "layout_tool", arguments: JSON.stringify({ command: `echo layout-${index}` }), call_id: `call-${index}` });
    event("response_item", { type: "function_call_output", call_id: `call-${index}`, output: { is_error: sessionId === "layout-error" && index === 24, content: `Layout result ${index}` } });
    event("event_msg", { type: "task_complete", turn_id: `turn-${index}` });
  }
  await writeFile(join(sessions, `${sessionId}.jsonl`), `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`);
}
async function unusedPort() {
  const listener = createServer();
  await new Promise((done, reject) => { listener.once("error", reject); listener.listen(0, "127.0.0.1", done); });
  const address = listener.address();
  assert.ok(address && typeof address !== "string");
  await new Promise((done) => listener.close(done));
  return address.port;
}

const port = process.env.LAYOUT_PORT ?? (process.argv.includes("--serve") ? 8127 : await unusedPort());
const url = `http://127.0.0.1:${port}`;
const python = process.env.LAYOUT_PYTHON ?? (process.platform === "win32" ? join(root, ".venv/Scripts/python.exe") : join(root, ".venv/bin/python"));
const logs = [];
const executable = desktop ? join(root, "frontend/src-tauri/target/release/codex-session-monitor-layout-qa.exe") : python;
const args = desktop ? [] : ["-m", "codex_monitor", "--project", project, "--session-root", sessions, "--port", port, "--no-saved-projects"];
const server = spawn(executable, args, {
  cwd: root,
  windowsHide: true,
  env: {
    ...process.env, LOCALAPPDATA: temporary, XDG_CONFIG_HOME: temporary,
    ...(desktop ? {
      USERPROFILE: temporary, HOME: temporary, APPDATA: temporary,
      WEBVIEW2_USER_DATA_FOLDER: join(temporary, "webview"),
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: "--remote-debugging-port=9223 --remote-debugging-address=127.0.0.1",
    } : {}),
  },
});
server.stdout.on("data", (data) => logs.push(String(data)));
server.stderr.on("data", (data) => logs.push(String(data)));
server.on("error", (error) => logs.push(error.message));
let browser;
try {
  let ready = false;
  for (let attempt = 0; attempt < 300; attempt += 1) {
    if (server.exitCode !== null) break;
    try {
      if ((await fetch(desktop ? "http://127.0.0.1:9223/json/version" : `${url}/api/sessions`)).ok) { ready = true; break; }
    } catch {}
    await new Promise((done) => setTimeout(done, 100));
  }
  assert.ok(ready, `Fixture server did not start (exit ${server.exitCode}, data ${temporary}): ${logs.join("")}`);
  console.log(`Layout fixture ready: ${desktop ? "Tauri / WebView2" : url}`);
  if (process.argv.includes("--serve")) await new Promise(() => {});
  browser = desktop ? await chromium.connectOverCDP("http://127.0.0.1:9223") : await chromium.launch({ channel: "msedge", headless: true });
  const context = browser.contexts()[0];
  const page = desktop ? (context.pages()[0] ?? await context.waitForEvent("page")) : await browser.newPage();
  await page.setViewportSize({ width: 1536, height: 800 });
  if (!desktop) await page.goto(url);
  else await page.waitForFunction(() => Boolean(window.__TAURI_INTERNALS__));
  const surface = await page.evaluate(() => ({ url: location.origin, tauri: Boolean(window.__TAURI_INTERNALS__), agent: navigator.userAgent }));
  console.log(`Rendering surface: ${JSON.stringify(surface)}`);
  assert.equal(surface.tauri, desktop, "Verification must use the requested rendering surface");
  const output = join(root, "output", "timeline-layout");
  await mkdir(output, { recursive: true });
  const measure = () => page.evaluate(() => {
    const workspace = document.querySelector(".workspace");
    const timeline = document.querySelector(".timeline");
    return {
      workspaceScroll: workspace.scrollTop,
      workspaceBottom: workspace.getBoundingClientRect().bottom,
      timelineBottom: timeline.getBoundingClientRect().bottom,
      timelineHeight: timeline.clientHeight,
      decisionTop: document.querySelector(".decision").getBoundingClientRect().top,
      workspaceTop: workspace.getBoundingClientRect().top,
    };
  });
  const verify = async (stage) => {
    const geometry = await measure();
    console.log(`${stage}: ${JSON.stringify(geometry)}`);
    await page.screenshot({ path: join(output, `${desktop ? "webview2" : "edge"}-${stage}.png`), fullPage: true });
    assert.equal(geometry.workspaceScroll, 0, `${stage}: attention navigation must not scroll the clipped workspace`);
    assert.ok(Math.abs(geometry.workspaceBottom - geometry.timelineBottom) <= 2, `${stage}: timeline must fill the workspace to its bottom; no blank strip`);
    assert.ok(geometry.decisionTop >= geometry.workspaceTop, `${stage}: the session header must not be clipped above the workspace`);
    assert.ok(geometry.timelineHeight > 80, `${stage}: the timeline must have usable height`);
  };
  for (const width of [1536, 1280]) {
    await page.setViewportSize({ width, height: 800 });
    for (const sessionId of ["layout-completed", "layout-error", "layout-completed", "layout-error"]) {
      await page.locator(".queue-row").filter({ hasText: sessionId }).click();
      await page.getByRole("heading", { name: sessionId, exact: true }).waitFor();
      await page.locator(".trace-card").first().waitFor({ state: "attached" });
      if (sessionId === "layout-error") {
        await page.waitForFunction(() => document.activeElement?.matches(".tool-error[tabindex='-1']"));
        const visibleTarget = await page.evaluate(() => {
          const target = document.activeElement.getBoundingClientRect();
          const timeline = document.querySelector(".timeline").getBoundingClientRect();
          const toolbar = document.querySelector(".timeline-actions").getBoundingClientRect();
          return { targetTop: target.top, toolbarBottom: toolbar.bottom, timelineBottom: timeline.bottom };
        });
        assert.ok(visibleTarget.targetTop >= visibleTarget.toolbarBottom - 2 && visibleTarget.targetTop < visibleTarget.timelineBottom, `The attention target heading must be inside the timeline, below the toolbar: ${JSON.stringify(visibleTarget)}`);
      }
      await verify(`${width}-${sessionId}`);
    }
  }
  await page.getByRole("button", { name: "回到最近", exact: true }).click();
  await page.locator(".latest-action").waitFor();
  await page.getByRole("button", { name: "跳到最新", exact: true }).click();
  await page.waitForFunction(() => {
    const timeline = document.querySelector(".timeline");
    return timeline.scrollHeight - timeline.clientHeight - timeline.scrollTop <= 2;
  });
  await verify("latest");
  await page.getByRole("button", { name: "回到顶部", exact: true }).click();
  await page.waitForFunction(() => document.querySelector(".timeline").scrollTop === 0);
  await verify("top");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "返回总览", exact: true }).click();
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await page.locator(".queue-row").filter({ hasText: "layout-error" }).click();
    await page.waitForFunction(() => document.activeElement?.matches(".tool-error[tabindex='-1']"));
    const mobile = await page.evaluate(() => {
      const target = document.activeElement.getBoundingClientRect();
      return { outerScroll: document.querySelector(".workspace").scrollTop, top: target.top, bottom: target.bottom, viewport: window.innerHeight };
    });
    assert.equal(mobile.outerScroll, 0);
    assert.ok(mobile.bottom > 0 && mobile.top < mobile.viewport, "Compact attention target must remain in the visible page");
    await page.screenshot({ path: join(output, `${desktop ? "webview2" : "edge"}-compact-${attempt}.png`) });
    await page.getByRole("button", { name: "返回总览", exact: true }).click();
  }
  console.log("PASS: attention navigation, session switching, Trace loading, latest/top scrolling");
} finally {
  await browser?.close();
  if (desktop && server.pid) {
    await new Promise((done) => spawn("taskkill", ["/PID", String(server.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" }).once("exit", done));
  } else server.kill();
}
