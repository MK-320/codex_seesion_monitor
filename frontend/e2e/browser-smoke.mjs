import { spawn } from "node:child_process";
import { access, appendFile, mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "@playwright/test";

const root = resolve(import.meta.dirname, "..", "..");
const temporary = await mkdtemp(join(tmpdir(), "codex-monitor-e2e-"));
const project = join(temporary, "project");
const imported = join(temporary, "imported");
const sessions = join(temporary, "sessions");
const SOAK_CYCLES = 3;
const ATTENTION_REGISTRY_KEY = "codex-monitor.attention-registry.v1";
const ATTENTION_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const SESSION_SUMMARY_READY_TIMEOUT_MS = 20_000;
const SESSION_SUMMARY_POLL_INTERVAL_MS = 200;

async function dismissAttention(page) {
  await page.getByRole("button", { name: /^(标记已处理|暂不提醒)$/ }).click();
}
await Promise.all([mkdir(project), mkdir(imported), mkdir(sessions)]);
const fixtureLines = [
  JSON.stringify({ timestamp: "2026-07-12T00:00:00Z", type: "session_meta", payload: { session_id: "e2e-session", cwd: project } }),
];
for (let index = 0; index < 125; index += 1) {
  const second = index * 3 + 1;
  const timestamp = new Date(Date.UTC(2026, 6, 12, 0, 0, second)).toISOString();
  fixtureLines.push(
    JSON.stringify({ timestamp, type: "event_msg", payload: { type: "task_started", turn_id: `turn-${index}`, started_at: 1783814401 + second } }),
    JSON.stringify({ timestamp, type: "event_msg", payload: { type: "user_message", message: `E2E message ${index}` } }),
    JSON.stringify({ timestamp, type: "event_msg", payload: { type: "task_complete", turn_id: `turn-${index}` } }),
  );
}
await writeFile(join(sessions, "session.jsonl"), `${fixtureLines.join("\n")}\n`, "utf8");
const attentionBaseMs = Date.now() - 10_000;
await writeFile(join(sessions, "initial-attention.jsonl"), [
  JSON.stringify({ timestamp: new Date(attentionBaseMs).toISOString(), type: "session_meta", payload: { session_id: "initial-attention", cwd: project } }),
  JSON.stringify({ timestamp: new Date(attentionBaseMs + 1000).toISOString(), type: "event_msg", payload: { type: "task_started", turn_id: "initial-turn", started_at: (attentionBaseMs + 1000) / 1000 } }),
  JSON.stringify({ timestamp: new Date(attentionBaseMs + 2000).toISOString(), type: "response_item", payload: { type: "function_call", name: "initial_tool", arguments: "{}", call_id: "initial-call" } }),
  JSON.stringify({ timestamp: new Date(attentionBaseMs + 3000).toISOString(), type: "response_item", payload: { type: "function_call_output", call_id: "initial-call", output: { is_error: true, content: "initial failure" } } }),
].join("\n") + "\n", "utf8");
for (const [filename, sessionId, toolName] of [
  ["batch-one.jsonl", "batch-one", "batch_one_tool"],
  ["batch-two.jsonl", "batch-two", "batch_two_tool"],
  ["unsafe-attention.jsonl", "unsafe;id", "unsafe_tool"],
]) {
  await writeFile(join(sessions, filename), [
    JSON.stringify({ timestamp: "2026-07-12T00:00:00Z", type: "session_meta", payload: { session_id: sessionId, cwd: project } }),
    JSON.stringify({ timestamp: "2026-07-12T00:00:01Z", type: "event_msg", payload: { type: "task_started", turn_id: `${sessionId}-turn`, started_at: 1783814401 } }),
    JSON.stringify({ timestamp: "2026-07-12T00:00:02Z", type: "response_item", payload: { type: "function_call", name: toolName, arguments: "{}", call_id: `${sessionId}-call` } }),
    JSON.stringify({ timestamp: "2026-07-12T00:00:03Z", type: "response_item", payload: { type: "function_call_output", call_id: `${sessionId}-call`, output: { is_error: true, content: `${sessionId} failure` } } }),
  ].join("\n") + "\n", "utf8");
}

let server;
let browser;
const logs = [];

function startServer() {
  const child = spawn("uv", ["run", "python", "-m", "codex_monitor", "--project", project, "--session-root", sessions, "--port", "8014", "--no-saved-projects"], { cwd: root });
  child.stdout.on("data", (chunk) => logs.push(chunk.toString()));
  child.stderr.on("data", (chunk) => logs.push(chunk.toString()));
  return child;
}

async function waitForServer() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch("http://127.0.0.1:8014/api/health");
      if (response.ok) return;
    } catch {}
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
  throw new Error(`server did not start\n${logs.join("")}`);
}

async function waitForSessionSummariesWithAttention(sessionIds) {
  const expectedIds = new Set(sessionIds);
  let lastSummaries = [];
  let lastHealth = null;
  const deadline = Date.now() + SESSION_SUMMARY_READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const healthResponse = await fetch("http://127.0.0.1:8014/api/health");
    if (healthResponse.ok) {
      lastHealth = await healthResponse.json();
      if (lastHealth.session_count < expectedIds.size || lastHealth.known_file_count < expectedIds.size) {
        await new Promise((resolveWait) => setTimeout(resolveWait, SESSION_SUMMARY_POLL_INTERVAL_MS));
        continue;
      }
    }
    const response = await fetch("http://127.0.0.1:8014/api/sessions");
    if (response.ok) {
      const summaries = await response.json();
      lastSummaries = summaries;
      const complete = [...expectedIds].every((sessionId) => {
        const eventKey = summaries.find((session) => session.session_id === sessionId)?.attention_context?.event_key;
        return typeof eventKey === "string" && /^[0-9a-f]{64}$/.test(eventKey);
      });
      if (complete) return summaries;
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, SESSION_SUMMARY_POLL_INTERVAL_MS));
  }
  throw new Error(
    `session attention contexts were not ready within ${SESSION_SUMMARY_READY_TIMEOUT_MS}ms: ${JSON.stringify({ health: lastHealth, summaries: lastSummaries })}`,
  );
}

async function stopServer() {
  if (!server || server.exitCode !== null) return;
  if (process.platform === "win32") {
    await new Promise((resolveExit) => {
      spawn("taskkill", ["/PID", String(server.pid), "/T", "/F"])
        .once("exit", resolveExit);
    });
  } else {
    server.kill("SIGTERM");
  }
  await Promise.race([
    new Promise((resolveExit) => server.once("exit", resolveExit)),
    new Promise((resolveWait) => setTimeout(resolveWait, 2000)),
  ]);
  if (server.exitCode === null && process.platform !== "win32") server.kill("SIGKILL");
}

async function waitForMetric(page, label, expected) {
  await page.waitForFunction(({ metricLabel, metricValue }) => [...document.querySelectorAll(".metrics span")]
    .some((element) => element.textContent?.includes(metricLabel) && element.querySelector("strong")?.textContent === String(metricValue)),
  { metricLabel: label, metricValue: expected });
}

async function waitForFocusedAttentionTarget(page, expectedText) {
  // Ensure the target content is painted before asserting focus ownership.
  await page.getByText(expectedText).first().waitFor({ state: "visible", timeout: 15_000 });
  await page.waitForFunction((text) => {
    const activeElement = document.activeElement;
    return activeElement instanceof HTMLElement
      && activeElement.tabIndex === -1
      && Boolean(activeElement.textContent?.includes(text));
  }, expectedText, { timeout: 15_000 });
}

async function openRegistryPage(entries, currentTime) {
  const registryPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await registryPage.addInitScript(({ storageKey, registryEntries, now }) => {
    Date.now = () => now;
    localStorage.setItem(storageKey, JSON.stringify({ schema_version: 1, entries: registryEntries }));
  }, { storageKey: ATTENTION_REGISTRY_KEY, registryEntries: entries, now: currentTime });
  await registryPage.goto("http://127.0.0.1:8014/");
  await registryPage.locator(".connection").getByText("实时连接").waitFor();
  return registryPage;
}

try {
  server = startServer();
  await waitForServer();
  const dashboardResponse = await fetch("http://127.0.0.1:8014/");
  if (dashboardResponse.headers.get("x-content-type-options") !== "nosniff" || dashboardResponse.headers.get("x-frame-options") !== "DENY" || !dashboardResponse.headers.get("content-security-policy")?.includes("frame-ancestors 'none'")) throw new Error("browser security headers were missing");
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const initialSessionSummaries = await waitForSessionSummariesWithAttention(["initial-attention", "batch-one", "batch-two", "unsafe;id"]);
  const initialAttention = initialSessionSummaries.find((session) => session.session_id === "initial-attention");
  const initialEventKey = initialAttention?.attention_context?.event_key;
  if (typeof initialEventKey !== "string" || !/^[0-9a-f]{64}$/.test(initialEventKey)) throw new Error("initial attention event key was missing");
  const batchOneOldEventKey = initialSessionSummaries.find((session) => session.session_id === "batch-one")?.attention_context?.event_key;
  const batchTwoOldEventKey = initialSessionSummaries.find((session) => session.session_id === "batch-two")?.attention_context?.event_key;
  const unsafeOldEventKey = initialSessionSummaries.find((session) => session.session_id === "unsafe;id")?.attention_context?.event_key;
  for (const [sessionId, eventKey] of [["batch-one", batchOneOldEventKey], ["batch-two", batchTwoOldEventKey], ["unsafe;id", unsafeOldEventKey]]) {
    if (typeof eventKey !== "string" || !/^[0-9a-f]{64}$/.test(eventKey)) throw new Error(`${sessionId} attention event key was missing`);
  }

  const registryNow = Date.now();
  const expiredPage = await openRegistryPage([{ event_key: initialEventKey, handled_at: registryNow - ATTENTION_TTL_MS }], registryNow);
  await waitForMetric(expiredPage, "未处理", 4);
  await waitForMetric(expiredPage, "已处理", 0);
  await expiredPage.close();

  const futurePage = await openRegistryPage([{ event_key: initialEventKey, handled_at: registryNow + 1 }], registryNow);
  await waitForMetric(futurePage, "未处理", 4);
  await waitForMetric(futurePage, "已处理", 0);
  await futurePage.close();

  const latestDuplicateTime = registryNow - 1000;
  const duplicatePage = await openRegistryPage([
    { event_key: initialEventKey, handled_at: registryNow - 2000 },
    { event_key: initialEventKey, handled_at: latestDuplicateTime },
  ], registryNow);
  await waitForMetric(duplicatePage, "已处理", 1);
  await duplicatePage.getByRole("group", { name: "筛选会话" }).getByRole("button", { name: "未处理" }).click();
  await duplicatePage.getByRole("button", { name: /batch-one/ }).click();
  await dismissAttention(duplicatePage);
  const deduplicatedRegistry = await duplicatePage.evaluate((storageKey) => JSON.parse(localStorage.getItem(storageKey)), ATTENTION_REGISTRY_KEY);
  const duplicateEntries = deduplicatedRegistry.entries.filter((entry) => entry.event_key === initialEventKey);
  if (duplicateEntries.length !== 1 || duplicateEntries[0].handled_at !== latestDuplicateTime) throw new Error("registry did not retain the newest duplicate");
  await duplicatePage.close();

  const stableEntries = [];
  for (let index = 0; stableEntries.length < 1001; index += 1) {
    const eventKey = index.toString(16).padStart(64, "0");
    if (eventKey !== initialEventKey) stableEntries.push({ event_key: eventKey, handled_at: registryNow });
  }
  stableEntries.reverse();
  const trimmedPage = await openRegistryPage(stableEntries, registryNow);
  await trimmedPage.getByRole("group", { name: "筛选会话" }).getByRole("button", { name: "未处理" }).click();
  await trimmedPage.getByRole("button", { name: /initial-attention/ }).click();
  await dismissAttention(trimmedPage);
  const trimmedRegistry = await trimmedPage.evaluate((storageKey) => JSON.parse(localStorage.getItem(storageKey)), ATTENTION_REGISTRY_KEY);
  const expectedStableKeys = stableEntries.map((entry) => entry.event_key).sort().slice(0, 999);
  if (
    trimmedRegistry.entries.length !== 1000 ||
    trimmedRegistry.entries[0].event_key !== initialEventKey ||
    JSON.stringify(trimmedRegistry.entries.slice(1).map((entry) => entry.event_key)) !== JSON.stringify(expectedStableKeys)
  ) throw new Error("registry trimming was not stable or did not retain the newly handled key");
  await trimmedPage.close();

  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  let updateDepthError = false;
  page.on("console", (message) => {
    if (message.text().includes("Maximum update depth exceeded")) updateDepthError = true;
  });
  await page.addInitScript(() => {
    localStorage.setItem("attention-notifications-v2", "true");
    window.__clipboardWrites = [];
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (text) => { window.__clipboardWrites.push(text); } } });
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    class NotificationStub {
      static permission = "granted";
      static calls = [];
      static async requestPermission() { return "granted"; }
      constructor(title, options) {
        this.title = title;
        this.body = options?.body ?? "";
        this.onclick = null;
        NotificationStub.calls.push(this);
      }
    }
    Object.defineProperty(window, "Notification", { configurable: true, value: NotificationStub });
  });
  await page.goto("http://127.0.0.1:8014/");
  await page.locator(".connection").getByText("实时连接").waitFor();
  if (await page.evaluate(() => window.Notification.calls.length) !== 0) throw new Error("initial snapshot sent a notification");
  await page.getByRole("button", { name: "关闭桌面提醒" }).waitFor();
  await page.route("**/api/config", (route) => route.fulfill({
    json: { projects: [
      { project_key: "project", project_root: project, project_name: "project", status: "active", persisted: false },
      { project_key: "missing", project_root: "C:\\redacted\\missing", project_name: "missing", status: "missing", persisted: true },
    ] },
  }));
  await page.reload();
  await page.getByText("仅本次").waitFor();
  await page.getByText("目录失效").waitFor();
  await page.unroute("**/api/config");
  await page.reload();
  await page.locator(".connection").getByText("实时连接").waitFor();
  const projectNavigation = page.getByRole("navigation", { name: "项目导航" });
  await projectNavigation.getByRole("button", { name: /^project\b/ }).click();
  await page.getByRole("button", { name: /e2e-session/ }).click();
  await page.getByRole("heading", { name: "e2e-session" }).waitFor();
  await projectNavigation.getByRole("button", { name: /全部项目/ }).click();
  const statusFilter = page.getByRole("group", { name: "筛选会话" });
  await waitForMetric(page, "未处理", 4);
  await waitForMetric(page, "已处理", 0);
  if (await projectNavigation.getByRole("button", { name: /全部项目/ }).locator("b").textContent() !== "4") throw new Error("project attention count did not show the unhandled total");
  await statusFilter.getByRole("button", { name: "未处理" }).click();
  const initialDetailUrl = "http://127.0.0.1:8014/api/sessions/project%3Ainitial-attention";
  const [initialDetailResponse, longDetailResponse] = await Promise.all([
    page.request.get(initialDetailUrl),
    page.request.get("http://127.0.0.1:8014/api/sessions/project%3Ae2e-session"),
  ]);
  if (!initialDetailResponse.ok() || !longDetailResponse.ok()) throw new Error("could not load real detail fixtures for the historical attention route");
  const initialDetail = await initialDetailResponse.json();
  const longDetail = await longDetailResponse.json();
  const initialTargetTurn = initialDetail.turns.find((turn) => turn.turn_id === "initial-turn");
  if (!initialTargetTurn || longDetail.turns.length !== 20) throw new Error("historical attention route fixtures were incomplete");
  const routedTurnCount = longDetail.turn_count + 1;
  const latestInitialDetail = {
    ...initialDetail,
    turn_count: routedTurnCount,
    turns: longDetail.turns,
    has_earlier: true,
    next_before: routedTurnCount - longDetail.turns.length,
  };
  const anchoredInitialDetail = {
    ...initialDetail,
    turn_count: routedTurnCount,
    turns: [initialTargetTurn, ...longDetail.turns.slice(0, 19)],
    has_earlier: false,
    next_before: null,
  };
  const initialDetailPattern = /\/api\/sessions\/project%3Ainitial-attention(?:\?.*)?$/i;
  let historicalAnchorRequests = 0;
  let historicalLatestRequests = 0;
  const historicalDetailRoute = async (route) => {
    const anchorTurnId = new URL(route.request().url()).searchParams.get("anchor_turn_id");
    if (anchorTurnId === "initial-turn") {
      historicalAnchorRequests += 1;
      await route.fulfill({ json: anchoredInitialDetail });
      return;
    }
    historicalLatestRequests += 1;
    await route.fulfill({ json: latestInitialDetail });
  };
  await page.route(initialDetailPattern, historicalDetailRoute);
  const initialAttentionRow = page.getByRole("button", { name: /initial-attention/ });
  await initialAttentionRow.click();
  const historicalLabel = page.getByText("历史定位视图", { exact: true });
  const resetLatestButton = page.getByRole("button", { name: "回到最近" });
  await historicalLabel.waitFor();
  await waitForFocusedAttentionTarget(page, "initial failure");
  if (historicalAnchorRequests !== 1 || historicalLatestRequests !== 0) throw new Error("opening attention detail did not issue exactly one initial-turn anchor request");
  await appendFile(join(sessions, "initial-attention.jsonl"), `${JSON.stringify({ timestamp: new Date().toISOString(), type: "response_item", payload: { type: "function_call", name: "ordinary_historical_update", arguments: "{}", call_id: "ordinary-historical-call" } })}\n`, "utf8");
  await initialAttentionRow.getByText(/^ordinary_historical_update · 已运行/).waitFor({ timeout: 10000 });
  if (historicalAnchorRequests !== 1 || historicalLatestRequests !== 0) throw new Error("ordinary websocket update issued a background detail request");
  await historicalLabel.waitFor();
  const historicalTarget = page.locator(".event").filter({ hasText: "initial failure" }).first();
  await historicalTarget.waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "返回总览" }).click();
  await initialAttentionRow.click();
  await historicalLabel.waitFor();
  for (const [label, locator] of [["historical label", historicalLabel], ["reset latest", resetLatestButton], ["historical target", historicalTarget]]) {
    const box = await locator.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > 390 || await locator.evaluate((element) => element.scrollWidth > element.clientWidth)) throw new Error(`${label} was not usable at 390px`);
  }
  await page.setViewportSize({ width: 1280, height: 900 });
  if (historicalAnchorRequests !== 2) throw new Error("reopening historical detail at compact width did not issue exactly one new anchor request");
  const latestRequestsBeforeReset = historicalLatestRequests;
  await resetLatestButton.click();
  await page.getByText("E2E message 124", { exact: true }).waitFor();
  await historicalLabel.waitFor({ state: "hidden" });
  if (historicalLatestRequests !== latestRequestsBeforeReset + 1) throw new Error("returning to latest did not issue exactly one unanchored request");
  await page.unroute(initialDetailPattern, historicalDetailRoute);
  await page.getByRole("heading", { name: "initial-attention" }).waitFor();
  await page.getByRole("button", { name: "复制 session ID" }).click();
  if (await page.evaluate(() => window.__clipboardWrites.at(-1)) !== "initial-attention") throw new Error("safe session ID copy failed");
  await dismissAttention(page);
  await page.waitForFunction(() => document.querySelector("main h2")?.textContent !== "initial-attention");
  if (await page.locator(".queue-row.selected").count() !== 1) throw new Error("handling a visible attention item did not select the next visible session");
  await waitForMetric(page, "未处理", 3);
  await waitForMetric(page, "已处理", 1);
  await page.reload();
  await page.locator(".connection").getByText("实时连接").waitFor();
  await waitForMetric(page, "未处理", 3);
  await waitForMetric(page, "已处理", 1);
  let anchorNotFoundRequests = 0;
  let anchorFallbackRequests = 0;
  const anchorNotFoundRoute = async (route) => {
    if (new URL(route.request().url()).searchParams.get("anchor_turn_id") === "initial-turn") {
      anchorNotFoundRequests += 1;
      await route.fulfill({ status: 404, json: { detail: "Attention anchor not found" } });
      return;
    }
    anchorFallbackRequests += 1;
    await route.continue();
  };
  await page.route(initialDetailPattern, anchorNotFoundRoute);
  await statusFilter.getByRole("button", { name: "已处理" }).click();
  await page.getByText("未找到历史位置，已显示最近详情", { exact: true }).waitFor();
  if (anchorNotFoundRequests !== 1 || anchorFallbackRequests !== 1) throw new Error("missing attention anchor did not fall back exactly once");
  await page.unroute(initialDetailPattern, anchorNotFoundRoute);

  let missingSessionAnchorRequests = 0;
  let missingSessionFallbackRequests = 0;
  const missingSessionRoute = async (route) => {
    if (new URL(route.request().url()).searchParams.get("anchor_turn_id") === "initial-turn") {
      missingSessionAnchorRequests += 1;
      await route.fulfill({ status: 404, json: { detail: "Session not found" } });
      return;
    }
    missingSessionFallbackRequests += 1;
    await route.continue();
  };
  await statusFilter.getByRole("button", { name: "未处理" }).click();
  await page.waitForFunction(() => document.querySelector("main h2")?.textContent !== "initial-attention");
  await page.route(initialDetailPattern, missingSessionRoute);
  await statusFilter.getByRole("button", { name: "已处理" }).click();
  await page.getByRole("alert").getByText("会话已从快照中移除", { exact: true }).waitFor();
  if (missingSessionAnchorRequests !== 1 || missingSessionFallbackRequests !== 0) throw new Error("missing session incorrectly fell back to latest detail");
  await page.unroute(initialDetailPattern, missingSessionRoute);
  await page.getByRole("button", { name: /initial-attention/ }).click();
  await page.getByRole("heading", { name: "initial-attention" }).waitFor();
  await page.getByRole("button", { name: "复制 codex resume" }).click();
  if (await page.evaluate(() => window.__clipboardWrites.at(-1)) !== "codex resume initial-attention") throw new Error("safe resume copy failed");
  const handledNotificationCount = await page.evaluate(() => window.Notification.calls.length);
  await appendFile(join(sessions, "initial-attention.jsonl"), `${JSON.stringify({ timestamp: new Date().toISOString(), type: "response_item", payload: { type: "function_call", name: "ordinary_attention_update", arguments: "{}", call_id: "ordinary-attention-call" } })}\n`, "utf8");
  await page.getByRole("button", { name: /initial-attention/ }).getByText(/^ordinary_attention_update · 已运行/).waitFor({ timeout: 10000 });
  if (await page.evaluate(() => window.Notification.calls.length) !== handledNotificationCount) throw new Error("ordinary increment for a handled attention event sent a notification");
  await waitForMetric(page, "未处理", 3);
  await waitForMetric(page, "已处理", 1);
  await appendFile(join(sessions, "initial-attention.jsonl"), [
    JSON.stringify({ timestamp: new Date(Date.now() + 1000).toISOString(), type: "response_item", payload: { type: "function_call", name: "new_failure_tool", arguments: "{}", call_id: "new-failure-call" } }),
    JSON.stringify({ timestamp: new Date(Date.now() + 2000).toISOString(), type: "response_item", payload: { type: "function_call_output", call_id: "new-failure-call", output: { is_error: true, content: "new failure output" } } }),
  ].join("\n") + "\n", "utf8");
  await page.getByRole("heading", { name: "选择一个会话" }).waitFor({ timeout: 10000 });
  await page.waitForFunction((count) => window.Notification.calls.length > count, handledNotificationCount);
  if (await page.evaluate(() => window.Notification.calls.length) !== handledNotificationCount + 1) throw new Error("a new attention event did not send exactly one notification");
  await page.evaluate(() => { window.Notification.calls.length = 0; });
  await waitForMetric(page, "未处理", 4);
  await waitForMetric(page, "已处理", 0);
  await statusFilter.getByRole("button", { name: "未处理" }).click();
  await page.getByRole("button", { name: /initial-attention/ }).click();
  await page.getByLabel("注意力上下文").getByText("new failure output", { exact: false }).waitFor();
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("batch-");
  const batchButton = page.getByRole("button", { name: "标记当前筛选为已处理 (2)" });
  page.once("dialog", (confirmation) => confirmation.accept());
  await batchButton.click();
  await page.getByText("当前筛选下没有会话").waitFor();
  await page.getByRole("heading", { name: "选择一个会话" }).waitFor();
  await waitForMetric(page, "未处理", 2);
  await waitForMetric(page, "已处理", 2);
  const batchUndoButton = page.getByRole("button", { name: "撤销最近一次批量处理 (2)" });
  await batchUndoButton.waitFor();
  await appendFile(join(sessions, "batch-one.jsonl"), [
    JSON.stringify({ timestamp: new Date(Date.now() + 1000).toISOString(), type: "response_item", payload: { type: "function_call", name: "batch_one_new_tool", arguments: "{}", call_id: "batch-one-new-call" } }),
    JSON.stringify({ timestamp: new Date(Date.now() + 2000).toISOString(), type: "response_item", payload: { type: "function_call_output", call_id: "batch-one-new-call", output: { is_error: true, content: "batch-one new failure output" } } }),
  ].join("\n") + "\n", "utf8");
  const batchOneRow = page.getByRole("button", { name: /batch-one/ });
  await batchOneRow.waitFor({ timeout: 10000 });
  await batchOneRow.click();
  await page.getByLabel("注意力上下文").getByText("batch-one new failure output", { exact: false }).waitFor();
  const updatedBatchOneResponse = await page.request.get("http://127.0.0.1:8014/api/sessions");
  if (!updatedBatchOneResponse.ok()) throw new Error("could not load the updated batch-one summary");
  const batchOneNewEventKey = (await updatedBatchOneResponse.json()).find((session) => session.session_id === "batch-one")?.attention_context?.event_key;
  if (typeof batchOneNewEventKey !== "string" || batchOneNewEventKey === batchOneOldEventKey) throw new Error("batch-one did not expose a new attention event key");
  await dismissAttention(page);
  await batchUndoButton.waitFor();
  await page.evaluate((storageKey) => {
    window.__attentionRegistryWrites = 0;
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key === storageKey) window.__attentionRegistryWrites += 1;
      return setItem.call(this, key, value);
    };
  }, ATTENTION_REGISTRY_KEY);
  await batchUndoButton.click();
  await page.getByRole("status").getByText("已撤销 2 个关注事件", { exact: true }).waitFor();
  await waitForMetric(page, "未处理", 3);
  await waitForMetric(page, "已处理", 1);
  if (await page.evaluate(() => window.__attentionRegistryWrites) !== 1) throw new Error("batch undo did not persist the registry exactly once");
  const registryAfterFirstUndo = await page.evaluate((storageKey) => JSON.parse(localStorage.getItem(storageKey)), ATTENTION_REGISTRY_KEY);
  const keysAfterFirstUndo = registryAfterFirstUndo.entries.map((entry) => entry.event_key);
  if (keysAfterFirstUndo.includes(batchOneOldEventKey) || keysAfterFirstUndo.includes(batchTwoOldEventKey) || !keysAfterFirstUndo.includes(batchOneNewEventKey)) throw new Error("batch undo did not remove only the original batch event keys");
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("batch-two");
  const batchTwoButton = page.getByRole("button", { name: "标记当前筛选为已处理 (1)" });
  page.once("dialog", (confirmation) => confirmation.accept());
  await batchTwoButton.click();
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("unsafe");
  const unsafeBatchButton = page.getByRole("button", { name: "标记当前筛选为已处理 (1)" });
  page.once("dialog", (confirmation) => confirmation.accept());
  await unsafeBatchButton.click();
  const latestBatchUndoButton = page.getByRole("button", { name: "撤销最近一次批量处理 (1)" });
  await latestBatchUndoButton.waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await latestBatchUndoButton.waitFor();
  const batchUndoBox = await latestBatchUndoButton.boundingBox();
  if (!batchUndoBox || batchUndoBox.x < 0 || batchUndoBox.x + batchUndoBox.width > 390 || await latestBatchUndoButton.evaluate((element) => element.scrollWidth > element.clientWidth)) throw new Error("batch undo control was not usable at 390px");
  await page.setViewportSize({ width: 1280, height: 900 });
  await latestBatchUndoButton.click();
  await latestBatchUndoButton.waitFor({ state: "hidden" });
  await page.getByRole("status").getByText("已撤销 1 个关注事件", { exact: true }).waitFor();
  await waitForMetric(page, "未处理", 2);
  await waitForMetric(page, "已处理", 2);
  const registryAfterLatestUndo = await page.evaluate((storageKey) => JSON.parse(localStorage.getItem(storageKey)), ATTENTION_REGISTRY_KEY);
  const keysAfterLatestUndo = registryAfterLatestUndo.entries.map((entry) => entry.event_key);
  if (keysAfterLatestUndo.includes(unsafeOldEventKey) || !keysAfterLatestUndo.includes(batchTwoOldEventKey) || !keysAfterLatestUndo.includes(batchOneNewEventKey)) throw new Error("latest batch undo did not preserve earlier handled events");
  await page.getByRole("button", { name: /unsafe;id/ }).click();
  await page.getByRole("button", { name: "复制 session ID" }).click();
  if (await page.evaluate(() => window.__clipboardWrites.at(-1)) !== "unsafe;id") throw new Error("unsafe session ID copy failed");
  if (!await page.getByRole("button", { name: "复制 codex resume" }).isDisabled()) throw new Error("unsafe resume copy was enabled");
  await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error("clipboard denied"); }; });
  const unsafeCopy = page.getByRole("button", { name: "复制 session ID" });
  await unsafeCopy.click();
  await unsafeCopy.getByRole("status").getByText("复制失败").waitFor();
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("");
  await Promise.all([
    writeFile(join(sessions, "aborted-attention.jsonl"), [
      JSON.stringify({ timestamp: "2026-07-12T00:10:00Z", type: "session_meta", payload: { session_id: "aborted-attention", cwd: project } }),
      JSON.stringify({ timestamp: "2026-07-12T00:10:01Z", type: "event_msg", payload: { type: "task_started", turn_id: "aborted-turn", started_at: 1783815001 } }),
      JSON.stringify({ timestamp: "2026-07-12T00:10:02Z", type: "event_msg", payload: { type: "turn_aborted", turn_id: "aborted-turn", reason: "E2E abort" } }),
    ].join("\n") + "\n", "utf8"),
    writeFile(join(sessions, "stuck-attention.jsonl"), [
      JSON.stringify({ timestamp: "2026-07-12T00:20:00Z", type: "session_meta", payload: { session_id: "stuck-attention", cwd: project } }),
      JSON.stringify({ timestamp: "2026-07-12T00:20:01Z", type: "event_msg", payload: { type: "task_started", turn_id: "stuck-turn", started_at: 1783815601 } }),
    ].join("\n") + "\n", "utf8"),
  ]);
  await page.getByRole("button", { name: /aborted-attention/ }).click();
  await page.getByText("任务中止", { exact: true }).waitFor();
  await waitForFocusedAttentionTarget(page, "aborted-turn");
  await page.getByRole("button", { name: /stuck-attention/ }).click();
  await page.getByLabel("注意力上下文").getByText("长时间没有新进展", { exact: true }).waitFor();
  await waitForFocusedAttentionTarget(page, "stuck-turn");
  const memoryPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await memoryPage.addInitScript(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key === "codex-monitor.attention-registry.v1") throw new Error("storage unavailable");
      return setItem.call(this, key, value);
    };
  });
  await memoryPage.goto("http://127.0.0.1:8014/");
  await memoryPage.locator(".connection").getByText("实时连接").waitFor();
  await memoryPage.getByRole("group", { name: "筛选会话" }).getByRole("button", { name: "未处理" }).click();
  await memoryPage.getByRole("button", { name: /initial-attention/ }).click();
  await dismissAttention(memoryPage);
  await memoryPage.getByLabel("会话定位").getByText("处理状态仅在本次打开期间保留", { exact: true }).waitFor();
  await memoryPage.close();
  const deniedStoragePage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await deniedStoragePage.addInitScript(() => {
    const denyStorage = () => { throw new DOMException("storage unavailable", "SecurityError"); };
    Storage.prototype.getItem = denyStorage;
    Storage.prototype.setItem = denyStorage;
  });
  await deniedStoragePage.goto("http://127.0.0.1:8014/");
  await deniedStoragePage.locator(".connection").getByText("实时连接").waitFor();
  await deniedStoragePage.getByRole("group", { name: "筛选会话" }).getByRole("button", { name: "未处理" }).click();
  await deniedStoragePage.getByRole("button", { name: /initial-attention/ }).click();
  await dismissAttention(deniedStoragePage);
  await deniedStoragePage.getByLabel("会话定位").getByText("处理状态仅在本次打开期间保留", { exact: true }).waitFor();
  await deniedStoragePage.close();
  const corruptPage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await corruptPage.addInitScript(() => localStorage.setItem("codex-monitor.attention-registry.v1", "{broken"));
  await corruptPage.goto("http://127.0.0.1:8014/");
  await corruptPage.locator(".connection").getByText("实时连接").waitFor();
  await corruptPage.getByRole("group", { name: "筛选会话" }).getByRole("button", { name: "未处理" }).click();
  await corruptPage.getByRole("button", { name: /initial-attention/ }).click();
  await dismissAttention(corruptPage);
  const repairedRegistry = await corruptPage.evaluate(() => JSON.parse(localStorage.getItem("codex-monitor.attention-registry.v1")));
  if (repairedRegistry.schema_version !== 1 || repairedRegistry.entries.length !== 1) throw new Error("corrupt registry was not replaced with the valid schema");
  await corruptPage.close();
  const largePage = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await largePage.routeWebSocket("**/ws", () => undefined);
  await largePage.route("**/api/sessions", (route) => route.fulfill({ json: Array.from({ length: 1001 }, (_, index) => ({
    session_key: `bulk-${index}`,
    session_id: `bulk-${index}`,
    project_key: "project",
    project_root: project,
    cwd: project,
    originator: "codex",
    source: "cli",
    cli_version: "0",
    model_provider: "openai",
    status: "idle",
    last_event_at: 1783814403,
    current_turn_id: null,
    current_turn_started_at: null,
    current_user_message_summary: "",
    current_action: "",
    turn_count: 1,
    approx_context_chars: 0,
    attention_reasons: ["tool_error"],
    attention_context: { reason: "tool_error", event_key: index.toString(16).padStart(64, "0"), occurred_at: 1783814403, turn_id: `bulk-turn-${index}`, tool_call_index: 0, tool_name: "bulk_tool" },
    parse_diagnostics: { unknown_event_count: 0, malformed_line_count: 0, oversized_line_count: 0 },
  })) }));
  await largePage.goto("http://127.0.0.1:8014/");
  const oversizedBatch = largePage.getByRole("button", { name: "标记当前筛选为已处理 (1001)" });
  await oversizedBatch.waitFor();
  if (!await oversizedBatch.isDisabled() || await oversizedBatch.getAttribute("title") !== "当前结果超过 1000 条，请缩小筛选范围") throw new Error("oversized batch was not rejected");
  await largePage.close();
  await page.getByRole("button", { name: "清除筛选" }).click();
  await page.reload();
  await page.locator(".connection").getByText("实时连接").waitFor();
  await statusFilter.getByRole("button", { name: "已完成" }).click();
  await page.getByRole("button", { name: /e2e-session/ }).click();
  await page.getByRole("heading", { name: "e2e-session" }).waitFor();
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("no-matching-session");
  await page.getByText("当前筛选下没有会话").waitFor();
  await page.waitForTimeout(100);
  if (updateDepthError) throw new Error("empty session queue caused an update loop");
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("");
  await statusFilter.getByRole("button", { name: "全部" }).click();
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("e2e-session");
  await page.getByRole("heading", { name: "e2e-session" }).waitFor();
  await page.getByRole("button", { name: /e2e-session/ }).click();
  if (await page.getByRole("button", { name: "返回总览" }).count()) throw new Error("desktop displayed the compact back action");
  const liveStartedAt = Date.now();
  await appendFile(join(sessions, "session.jsonl"), [
    JSON.stringify({ timestamp: new Date().toISOString(), type: "event_msg", payload: { type: "task_started", turn_id: "turn-live", started_at: liveStartedAt / 1000 } }),
    JSON.stringify({ timestamp: new Date().toISOString(), type: "event_msg", payload: { type: "user_message", message: "E2E live update" } }),
  ].join("\n") + "\n", "utf8");
  await page.getByRole("main").getByRole("paragraph").filter({ hasText: "E2E live update" }).waitFor({ timeout: 10000 });
  for (let index = 0; index < 100; index += 1) {
    await appendFile(join(sessions, "session.jsonl"), `${JSON.stringify({ timestamp: new Date().toISOString(), type: "event_msg", payload: { type: "user_message", message: `E2E burst ${index}` } })}\n`, "utf8");
  }
  await page.getByRole("main").getByRole("paragraph").filter({ hasText: "E2E burst 99" }).waitFor({ timeout: 10000 });
  const newSessionNotificationCount = await page.evaluate(() => window.Notification.calls.length);
  await appendFile(join(sessions, "session.jsonl"), [
    JSON.stringify({ timestamp: new Date().toISOString(), type: "event_msg", payload: { type: "task_started", turn_id: "turn-notify", started_at: Date.now() / 1000 } }),
    JSON.stringify({ timestamp: new Date().toISOString(), type: "response_item", payload: { type: "function_call", name: "private_tool", arguments: "secret-input", call_id: "call-notify" } }),
    JSON.stringify({ timestamp: new Date().toISOString(), type: "response_item", payload: { type: "function_call_output", call_id: "call-notify", output: { is_error: true, content: "secret-output" } } }),
  ].join("\n") + "\n", "utf8");
  await page.waitForFunction((count) => window.Notification.calls.length === count + 1, newSessionNotificationCount);
  const notification = await page.evaluate(() => window.Notification.calls.at(-1));
  if (notification.title !== "Codex Session Monitor" || notification.body !== "有 1 个会话需要关注") throw new Error("unexpected notification content");
  if (JSON.stringify(notification).includes("secret") || JSON.stringify(notification).includes("e2e-session")) throw new Error("notification leaked session data");
  await projectNavigation.getByRole("button", { name: /^project\b/ }).click();
  await statusFilter.getByRole("button", { name: "已处理" }).click();
  await page.getByRole("searchbox", { name: "搜索会话" }).fill("initial-attention");
  await page.getByLabel("时间范围").selectOption("week");
  await page.getByLabel("会话排序").selectOption("oldest");
  await page.getByRole("button", { name: "折叠会话列" }).click();
  await page.evaluate(() => window.Notification.calls.at(-1).onclick?.());
  await page.waitForFunction(() => window.location.search === "?filter=attention");
  await page.getByRole("button", { name: "折叠会话列" }).waitFor();
  await page.getByRole("heading", { name: "e2e-session" }).waitFor();
  if (await page.locator(".queue-row.selected").count() !== 1) throw new Error("single-event notification activation did not select its exact live attention session");
  await appendFile(join(sessions, "session.jsonl"), `${JSON.stringify({ timestamp: new Date().toISOString(), type: "event_msg", payload: { type: "user_message", message: "secret-repeat" } })}\n`, "utf8");
  await page.waitForTimeout(500);
  if (await page.evaluate(() => window.Notification.calls.length) !== newSessionNotificationCount + 1) throw new Error("cooldown allowed a duplicate notification");
  const workspace = page.getByRole("main");
  const initialWorkspaceWidth = (await workspace.boundingBox())?.width ?? 0;
  await page.getByRole("button", { name: "折叠项目列" }).click();
  await page.getByRole("button", { name: "折叠会话列" }).click();
  const workspaceBox = await workspace.boundingBox();
  if (!workspaceBox || workspaceBox.width <= initialWorkspaceWidth) throw new Error("collapsed columns did not expand the workspace");
  await page.getByRole("navigation", { name: "项目导航" }).waitFor({ state: "hidden" });
  await page.getByRole("complementary", { name: "会话队列" }).waitFor({ state: "hidden" });
  if (process.env.CODEX_MONITOR_SCREENSHOTS === "1") await page.screenshot({ path: join(root, "frontend", "v511-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "展开项目列" }).click();
  await page.getByRole("button", { name: "展开会话列" }).click();
  await page.getByRole("navigation", { name: "项目导航" }).waitFor();
  await page.getByRole("complementary", { name: "会话队列" }).waitFor();
  for (let pageIndex = 0; pageIndex < 5; pageIndex += 1) {
    await Promise.all([
      page.waitForResponse((response) => response.url().includes("before=")),
      page.getByRole("button", { name: "加载更早", exact: true }).click(),
    ]);
  }
  if (await page.locator("details.turn").count() !== 60) throw new Error("long timeline did not retain its 60-turn window");
  await page.getByRole("button", { name: "回到最近" }).click();
  await page.waitForFunction(() => document.querySelectorAll("details.turn").length === 20);
  const timeline = page.locator(".timeline");
  await timeline.evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
  await page.waitForFunction(() => (document.querySelector(".timeline")?.scrollTop ?? 0) > 0);
  await page.getByRole("button", { name: "回到顶部" }).click();
  await page.waitForFunction(() => window.scrollY === 0 && document.querySelector(".timeline")?.scrollTop === 0);
  await page.route("**/api/projects/pick", (route) => route.fulfill({ status: 204 }));
  await page.getByRole("button", { name: "选择多个文件夹并导入项目" }).click();
  await page.getByRole("button", { name: "选择多个文件夹并导入项目" }).waitFor();
  if (await page.getByRole("alert").count()) throw new Error("cancelled import showed an error");
  const rejectedExport = await page.request.post("http://127.0.0.1:8014/api/diagnostics/export", { data: { confirmed: false } });
  if (rejectedExport.status() !== 422) throw new Error("unconfirmed diagnostic export was accepted");
  await page.getByRole("button", { name: "设置" }).click();
  await page.getByRole("heading", { name: "设置" }).waitFor();
  await page.getByRole("button", { name: "日志", exact: true }).click();
  await page.getByRole("heading", { name: "本机诊断日志" }).waitFor();
  await page.getByRole("button", { name: "导出全部日志" }).waitFor();
  await page.getByRole("button", { name: "关闭设置" }).click();
  await page.getByText("实时连接").waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("navigation", { name: "移动端导航" }).waitFor();
  const compactNotificationCount = await page.evaluate(() => {
    const now = Date.now();
    Date.now = () => now + 61_000;
    return window.Notification.calls.length;
  });
  const compactEventTime = Date.now();
  await writeFile(join(sessions, "compact-notification.jsonl"), [
    JSON.stringify({ timestamp: new Date(compactEventTime).toISOString(), type: "session_meta", payload: { session_id: "compact-notification", cwd: project } }),
    JSON.stringify({ timestamp: new Date(compactEventTime + 1000).toISOString(), type: "event_msg", payload: { type: "task_started", turn_id: "compact-turn", started_at: (compactEventTime + 1000) / 1000 } }),
    JSON.stringify({ timestamp: new Date(compactEventTime + 2000).toISOString(), type: "response_item", payload: { type: "function_call", name: "compact_tool", arguments: "{}", call_id: "compact-call" } }),
    JSON.stringify({ timestamp: new Date(compactEventTime + 3000).toISOString(), type: "response_item", payload: { type: "function_call_output", call_id: "compact-call", output: { is_error: true, content: "compact failure" } } }),
  ].join("\n") + "\n", "utf8");
  await page.waitForFunction((count) => window.Notification.calls.length === count + 1, compactNotificationCount);
  await page.evaluate(() => window.Notification.calls.at(-1).onclick?.());
  await page.getByRole("heading", { name: "compact-notification" }).waitFor();
  await page.getByRole("button", { name: "返回总览" }).waitFor();
  if (await page.getByRole("complementary", { name: "会话队列" }).count()) throw new Error("compact single-event notification left the hidden queue visible");
  await page.getByRole("button", { name: "返回总览" }).click();
  await page.getByRole("complementary", { name: "会话队列" }).waitFor();
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.waitForFunction(() => document.querySelectorAll(".queue-row.selected").length === 1);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("navigation", { name: "移动端导航" }).waitFor();
  for (const control of [
    statusFilter.getByRole("button", { name: "未处理" }),
    page.getByRole("button", { name: /标记当前筛选为已处理/ }),
  ]) {
    const box = await control.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > 390 || await control.evaluate((element) => element.scrollWidth > element.clientWidth)) throw new Error("attention control was not usable at 390px");
  }
  await page.getByRole("button", { name: "查看项目" }).click();
  await page.getByRole("navigation", { name: "项目导航" }).waitFor();
  if (await page.getByRole("complementary", { name: "会话队列" }).count() || await page.getByRole("main").count()) throw new Error("compact project pane rendered extra workspace panels");
  await page.getByRole("button", { name: "查看会话" }).click();
  await page.getByRole("complementary", { name: "会话队列" }).waitFor();
  await page.getByRole("button", { name: /e2e-session/ }).click();
  await page.getByRole("button", { name: "返回总览" }).waitFor();
  if (await page.getByRole("navigation", { name: "项目导航" }).count() || await page.getByRole("complementary", { name: "会话队列" }).count()) throw new Error("compact detail kept overview panels visible");
  if (process.env.CODEX_MONITOR_SCREENSHOTS === "1") await page.screenshot({ path: join(root, "frontend", "v511-mobile.png") });
  await page.getByRole("button", { name: "返回总览" }).click();
  for (const viewport of [{ width: 390, height: 844 }, { width: 768, height: 1024 }, { width: 1280, height: 900 }, { width: 1920, height: 1080 }]) {
    await page.setViewportSize(viewport);
    const hasOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    if (hasOverflow) throw new Error(`horizontal overflow at ${viewport.width}x${viewport.height}`);
  }
  const importedResponse = await page.request.post("http://127.0.0.1:8014/api/projects", { data: { project_root: imported } });
  if (importedResponse.status() !== 201) throw new Error("project import failed");
  const duplicateResponse = await page.request.post("http://127.0.0.1:8014/api/projects", { data: { project_root: imported } });
  if (duplicateResponse.status() !== 200) throw new Error("duplicate import was not idempotent");
  await page.reload();
  await page.locator(".connection").getByText("实时连接").waitFor();
  await page.getByRole("button", { name: /imported/ }).getByText("仅本次", { exact: true }).waitFor();
  page.once("dialog", (confirmation) => confirmation.accept());
  await page.getByRole("button", { name: "移除项目 imported" }).click();
  await page.getByRole("button", { name: "移除项目 imported" }).waitFor({ state: "detached" });
  await page.getByRole("button", { name: /e2e-session/ }).click();
  const soakResetLatestButton = page.getByRole("button", { name: "回到最近" });
  await soakResetLatestButton.waitFor();
  await soakResetLatestButton.click();
  await page.getByText("历史定位视图", { exact: true }).waitFor({ state: "hidden" });
  await access(imported);
  for (let cycle = 1; cycle <= SOAK_CYCLES; cycle += 1) {
    const marker = `E2E soak ${cycle}`;
    await appendFile(join(sessions, "session.jsonl"), `${JSON.stringify({ timestamp: new Date(Date.UTC(2026, 6, 12, 0, 5, cycle)).toISOString(), type: "event_msg", payload: { type: "user_message", message: marker } })}\n`, "utf8");
    await page.getByRole("main").getByRole("paragraph").filter({ hasText: marker }).waitFor({ timeout: 10000 });
    await stopServer();
    await page.locator(".connection").getByText("数据可能过期").waitFor({ timeout: 10000 });
    server = startServer();
    await waitForServer();
    await page.locator(".connection").getByText("实时连接").waitFor({ timeout: 10000 });
    await page.getByRole("button", { name: "关闭桌面提醒" }).waitFor();
  }
  console.log("browser smoke passed");
} catch (error) {
  console.error(logs.join(""));
  throw error;
} finally {
  await browser?.close();
  await stopServer();
}
