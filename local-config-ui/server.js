const childProcess = require("node:child_process");
const fs = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const { promisify } = require("node:util");
const {
  DEFAULT_OWNER,
  DEFAULT_REPO,
  DEFAULT_REF,
  DEFAULT_WORKFLOW,
  assignPlatformAdditionSlots,
  buildEnvPreview,
  KEEP_EXISTING_SECRET,
  mergeExistingSecretValues,
  parseWranglerToml,
  buildSecrets,
  buildWranglerToml,
  listPlatformAccounts,
  validateSettings,
  platformSecretNames,
  platformSlotLabel,
} = require("./config-service");
const { deleteRepoSecret, dispatchWorkflow, getRepoFile, listRepoSecrets, listWorkflowRuns, putRepoFile, putRepoSecret } = require("./github-service");
const { deriveHealth, summarizeManuscripts, unarchiveRecord } = require("./status-service");

const execFile = promisify(childProcess.execFile);
const HOST = "127.0.0.1";
const PORT = Number(process.env.PORT || 8976);
const ROOT_DIR = path.resolve(__dirname, "..");
const PUBLIC_DIR = path.join(__dirname, "public");
const WRANGLER_PATH = path.join(ROOT_DIR, "cloudflare-scheduler", "wrangler.toml");
const LOCAL_DATA_DIR = path.join(__dirname, "data");
const SLOT_ASSIGNMENTS_PATH = path.join(LOCAL_DATA_DIR, "platform-slot-assignments.json");
const PLATFORM_METADATA_PATH = path.join(LOCAL_DATA_DIR, "platform-metadata.json");

function jsonResponse(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function textResponse(res, statusCode, content, contentType = "text/plain; charset=utf-8", extraHeaders = {}) {
  res.writeHead(statusCode, {
    "Content-Type": contentType,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    ...extraHeaders,
  });
  res.end(content);
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  if (!chunks.length) {
    return {};
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

async function readSlotAssignments() {
  try {
    return JSON.parse(await fs.readFile(SLOT_ASSIGNMENTS_PATH, "utf8"));
  } catch {
    return {};
  }
}

async function writeSlotAssignments(assignments) {
  await fs.mkdir(LOCAL_DATA_DIR, { recursive: true });
  await fs.writeFile(SLOT_ASSIGNMENTS_PATH, JSON.stringify(assignments, null, 2), "utf8");
}

async function readPlatformMetadata() {
  try {
    return JSON.parse(await fs.readFile(PLATFORM_METADATA_PATH, "utf8"));
  } catch {
    return {};
  }
}

async function writePlatformMetadata(metadata) {
  await fs.mkdir(LOCAL_DATA_DIR, { recursive: true });
  await fs.writeFile(PLATFORM_METADATA_PATH, JSON.stringify(metadata, null, 2), "utf8");
}

function collectMetadataFromSettings(settings, currentMetadata = {}) {
  const updates = {};
  const platforms = settings?.platforms || {};
  for (const [platform, account] of Object.entries(platforms)) {
    if (!account) {
      continue;
    }
    const label = platformSlotLabel(platform, 1);
    updates[label] = {
      platform,
      slot: 1,
      email: account.email || currentMetadata[label]?.email || "",
      url: account.url || currentMetadata[label]?.url || "",
      updatedAt: new Date().toISOString(),
    };
  }
  const additions = Array.isArray(settings?.platformAccounts?.additions) ? settings.platformAccounts.additions : [];
  for (const account of additions) {
    if (!Number.isInteger(account.slot)) {
      continue;
    }
    const label = platformSlotLabel(account.platform, account.slot);
    updates[label] = {
      platform: account.platform,
      slot: account.slot,
      email: account.email || currentMetadata[label]?.email || "",
      url: account.url || currentMetadata[label]?.url || "",
      updatedAt: new Date().toISOString(),
    };
  }
  return updates;
}

function stripDeletedAssignments(assignments, label) {
  return Object.fromEntries(
    Object.entries(assignments || {}).filter(([, value]) => {
      if (!value || !Number.isInteger(value.slot)) {
        return true;
      }
      try {
        return platformSlotLabel(value.platform, value.slot) !== label;
      } catch {
        return true;
      }
    }),
  );
}

function repoFromSettings(settings) {
  return {
    owner: settings?.repository?.owner?.trim() || DEFAULT_OWNER,
    repo: settings?.repository?.name?.trim() || DEFAULT_REPO,
    workflow: settings?.repository?.workflow?.trim() || DEFAULT_WORKFLOW,
    ref: settings?.repository?.ref?.trim() || DEFAULT_REF,
  };
}

function assertLocalRequest(req) {
  const remote = req.socket.remoteAddress;
  if (remote && !["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(remote)) {
    throw new Error("Only local requests are allowed.");
  }
  // A socket address check alone does not stop DNS rebinding: a hostile page can
  // point its own domain at 127.0.0.1 and reach this server from the browser.
  // Requiring the Host header to be this exact listener blocks that.
  const host = String(req.headers.host || "").toLowerCase();
  const allowed = new Set([`${HOST}:${PORT}`, `localhost:${PORT}`]);
  if (!allowed.has(host)) {
    throw new Error("Unexpected Host header; refusing to answer.");
  }
}

function userFacingError(error) {
  const message = error?.message || String(error);
  if (message === "Bad credentials") {
    return "GitHub Token 无效或已过期，请重新生成 token。";
  }
  if (message.includes("Resource not accessible by personal access token")) {
    return "GitHub Token 缺少权限，至少需要 Secrets: Read and write，并授权到当前仓库。";
  }
  if (message.includes("incomplete input")) {
    return "GitHub Secrets 公钥解析失败。请确认 GitHub Token 有 Secrets 读写权限，然后重启本地配置后台后再保存。";
  }
  if (message.includes("Cannot find module 'libsodium-wrappers'")) {
    return "缺少依赖 libsodium-wrappers：在本目录运行 npm install，然后重新打开桌面快捷方式。";
  }
  if (message.includes("ENOTFOUND") || message.includes("EAI_AGAIN")) {
    return `连不上 GitHub（网络或 DNS 问题）：${message}`;
  }
  return message;
}

async function serveStatic(req, res) {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  const target = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
  const safePath = path.normalize(target).replace(/^(\.\.[/\\])+/, "");
  const filePath = path.join(PUBLIC_DIR, safePath);
  if (!filePath.startsWith(PUBLIC_DIR)) {
    textResponse(res, 403, "Forbidden");
    return;
  }
  try {
    const content = await fs.readFile(filePath);
    const ext = path.extname(filePath).toLowerCase();
    const type = ext === ".css" ? "text/css; charset=utf-8" : ext === ".js" ? "text/javascript; charset=utf-8" : "text/html; charset=utf-8";
    // The page loads only same-origin assets, so a strict policy is free here and
    // blocks any injected third-party script in a page that holds a GitHub token.
    const extra = type.startsWith("text/html")
      ? { "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; form-action 'none'; frame-ancestors 'none'" }
      : {};
    textResponse(res, 200, content, type, extra);
  } catch {
    textResponse(res, 404, "Not found");
  }
}

async function handlePreview(req, res) {
  const body = await readJson(req);
  const settings = body.settings || {};
  const existingSecrets = body.existingSecrets || {};
  const slotState = assignPlatformAdditionSlots(settings, existingSecrets, {});
  const settingsWithSlots = slotState.settings;
  const errors = validateSettings(settingsWithSlots, { existingSecrets });
  const mergedSecrets = mergeExistingSecretValues(settingsWithSlots, existingSecrets);
  jsonResponse(res, errors.length ? 400 : 200, {
    ok: errors.length === 0,
    errors,
    secrets: Object.keys(mergedSecrets)
      .filter((name) => mergedSecrets[name] !== KEEP_EXISTING_SECRET)
      .sort(),
    keptSecrets: Object.keys(mergedSecrets)
      .filter((name) => mergedSecrets[name] === KEEP_EXISTING_SECRET)
      .sort(),
    wranglerToml: buildWranglerToml(settingsWithSlots),
    envPreview: buildEnvPreview(settingsWithSlots, existingSecrets),
  });
}

async function handleCurrent(req, res) {
  const body = await readJson(req);
  const token = body.githubToken?.trim();
  let parsed = {};
  try {
    parsed = parseWranglerToml(await fs.readFile(WRANGLER_PATH, "utf8"));
  } catch {
    parsed = parseWranglerToml("");
  }

  let existingSecrets = {};
  let accounts = [];
  if (token) {
    const repo = {
      owner: body.repository?.owner?.trim() || parsed.repository?.owner || DEFAULT_OWNER,
      repo: body.repository?.name?.trim() || parsed.repository?.name || DEFAULT_REPO,
    };
    existingSecrets = await listRepoSecrets({
      owner: repo.owner,
      repo: repo.repo,
      token,
    });
    accounts = listPlatformAccounts(existingSecrets, await readPlatformMetadata());
  }

  jsonResponse(res, 200, {
    ok: true,
    repository: parsed.repository,
    schedule: parsed.schedule,
    existingSecrets,
    accounts,
  });
}

async function handleAccounts(req, res) {
  const body = await readJson(req);
  const token = body.githubToken?.trim();
  if (!token) {
    jsonResponse(res, 400, { ok: false, errors: ["GitHub token 必填，用于读取账号配置状态。"] });
    return;
  }
  const parsed = parseWranglerToml(await fs.readFile(WRANGLER_PATH, "utf8").catch(() => ""));
  const repo = {
    owner: body.repository?.owner?.trim() || parsed.repository?.owner || DEFAULT_OWNER,
    repo: body.repository?.name?.trim() || parsed.repository?.name || DEFAULT_REPO,
  };
  const existingSecrets = await listRepoSecrets({ owner: repo.owner, repo: repo.repo, token });
  jsonResponse(res, 200, {
    ok: true,
    existingSecrets,
    accounts: listPlatformAccounts(existingSecrets, await readPlatformMetadata()),
  });
}

async function handleSave(req, res) {
  const body = await readJson(req);
  const settings = body.settings || {};
  const tokens = body.tokens || {};
  const actions = body.actions || {};
  if (!tokens.githubToken?.trim()) {
    jsonResponse(res, 400, { ok: false, errors: ["GitHub token 必填，用于写入 GitHub Secrets。"] });
    return;
  }

  const repo = repoFromSettings(settings);
  const liveExistingSecrets = await listRepoSecrets({
    owner: repo.owner,
    repo: repo.repo,
    token: tokens.githubToken.trim(),
  });
  const existingSecrets = { ...(body.existingSecrets || {}), ...liveExistingSecrets };
  const previousAssignments = await readSlotAssignments();
  const slotState = assignPlatformAdditionSlots(settings, existingSecrets, previousAssignments);
  const settingsWithSlots = slotState.settings;
  const errors = validateSettings(settingsWithSlots, { existingSecrets });
  if (errors.length) {
    jsonResponse(res, 400, { ok: false, errors });
    return;
  }

  const secrets = mergeExistingSecretValues(settingsWithSlots, existingSecrets);
  await writeSlotAssignments(slotState.assignments);
  const updatedSecrets = [];
  const keptSecrets = [];
  for (const [name, value] of Object.entries(secrets)) {
    if (value === KEEP_EXISTING_SECRET) {
      keptSecrets.push(name);
      continue;
    }
    await putRepoSecret({
      owner: repo.owner,
      repo: repo.repo,
      token: tokens.githubToken.trim(),
      name,
      value,
    });
    updatedSecrets.push(name);
  }

  await fs.writeFile(WRANGLER_PATH, buildWranglerToml(settingsWithSlots), "utf8");
  const currentMetadata = await readPlatformMetadata();
  const nextMetadata = {
    ...currentMetadata,
    ...collectMetadataFromSettings(settingsWithSlots, currentMetadata),
  };
  await writePlatformMetadata(nextMetadata);

  let deployResult = null;
  if (actions.deployCloudflare) {
    if (!tokens.cloudflareToken?.trim()) {
      jsonResponse(res, 400, { ok: false, errors: ["勾选部署 Cloudflare 时，Cloudflare API Token 必填。"] });
      return;
    }
    deployResult = await deployCloudflare(tokens.cloudflareToken.trim());
  }

  let workflowResult = null;
  if (actions.dispatchTest) {
    workflowResult = await dispatchWorkflow({
      owner: repo.owner,
      repo: repo.repo,
      workflow: repo.workflow,
      ref: repo.ref,
      token: tokens.githubToken.trim(),
      mode: "test",
    });
  }

  const refreshedSecrets = await listRepoSecrets({
    owner: repo.owner,
    repo: repo.repo,
    token: tokens.githubToken.trim(),
  });

  jsonResponse(res, 200, {
    ok: true,
    updatedSecrets: updatedSecrets.sort(),
    keptSecrets: keptSecrets.sort(),
    wranglerPath: WRANGLER_PATH,
    deployResult,
    workflowResult,
    existingSecrets: refreshedSecrets,
    accounts: listPlatformAccounts(refreshedSecrets, nextMetadata),
  });
}

async function handleDeleteAccount(req, res) {
  const body = await readJson(req);
  const token = body.githubToken?.trim();
  if (!token) {
    jsonResponse(res, 400, { ok: false, errors: ["GitHub token 必填，用于删除 GitHub Secrets。"] });
    return;
  }
  const platform = body.account?.platform;
  const slot = Number.parseInt(body.account?.slot, 10);
  if (!Number.isInteger(slot)) {
    jsonResponse(res, 400, { ok: false, errors: ["删除账号时缺少有效槽位。"] });
    return;
  }
  const repo = {
    owner: body.repository?.owner?.trim() || DEFAULT_OWNER,
    repo: body.repository?.name?.trim() || DEFAULT_REPO,
  };
  const names = platformSecretNames(platform, slot);
  const deletedSecrets = [];
  for (const name of [names.email, names.password, names.url]) {
    const result = await deleteRepoSecret({
      owner: repo.owner,
      repo: repo.repo,
      token,
      name,
    });
    deletedSecrets.push(result.name);
  }

  const label = platformSlotLabel(platform, slot);
  const metadata = await readPlatformMetadata();
  delete metadata[label];
  await writePlatformMetadata(metadata);
  await writeSlotAssignments(stripDeletedAssignments(await readSlotAssignments(), label));

  jsonResponse(res, 200, {
    ok: true,
    deletedSecrets,
    label,
  });
}

// The manuscript state lives in a private repository: this server and the
// workflow both read it, and neither stores it in the public code repo.
const STATE_REPO = String(process.env.STATE_REPO || "Xuecheng377/data_storage").trim();

function stateRepoCoordinates() {
  const [owner, repo] = STATE_REPO.split("/");
  return { owner: owner || "Xuecheng377", repo: repo || "data_storage" };
}

async function handleStatus(req, res) {
  const body = await readJson(req);
  const token = String(body.githubToken || "").trim();
  const { owner, repo } = repoFromSettings(body.settings || {});
  if (!token) {
    jsonResponse(res, 400, { ok: false, errors: ["需要 GitHub Token 才能读取运行状态"] });
    return;
  }
  let schedule = {};
  try {
    const parsed = parseWranglerToml(await fs.readFile(WRANGLER_PATH, "utf8"));
    schedule = {
      NORMAL_HOURS: (parsed.schedule?.normalChecks?.hours || []).join(","),
      WINDOW_MINUTE: parsed.schedule?.normalChecks?.minute,
      WEEKLY_REPORT_DAY: parsed.schedule?.weeklyReport?.dayOfWeek,
      DAILY_REPORT_HOUR: parsed.schedule?.weeklyReport?.hour,
    };
  } catch (error) {
    schedule = {};
  }
  let runs = [];
  let runsError = "";
  try {
    runs = await listWorkflowRuns({ owner, repo, token, workflow: DEFAULT_WORKFLOW, perPage: 20 });
  } catch (error) {
    runsError = userFacingError(error);
  }
  let manuscripts = { active: [], archived: [], counts: { total: 0, active: 0, archived: 0 } };
  let dataError = "";
  try {
    const stateRepo = stateRepoCoordinates();
    const file = await getRepoFile({ ...stateRepo, token, path: "data/manuscripts.json", ref: DEFAULT_REF });
    manuscripts = file.exists ? summarizeManuscripts(JSON.parse(file.content)) : manuscripts;
  } catch (error) {
    dataError = userFacingError(error);
  }
  const health = deriveHealth({ runs, schedule });
  jsonResponse(res, 200, {
    ok: true,
    repo: `${owner}/${repo}`,
    schedule,
    health,
    // health.runs carries the normalized shape (including the manual flag).
    runs: health.runs,
    manuscripts,
    runsError,
    dataError,
  });
}

async function handleManuscripts(req, res) {
  const body = await readJson(req);
  const token = String(body.githubToken || "").trim();
  const { owner, repo } = repoFromSettings(body.settings || {});
  if (!token) {
    jsonResponse(res, 400, { ok: false, errors: ["需要 GitHub Token 才能读取稿件列表"] });
    return;
  }
  const file = await getRepoFile({
    ...stateRepoCoordinates(),
    token,
    path: "data/manuscripts.json",
    ref: DEFAULT_REF,
  });
  const data = file.exists ? JSON.parse(file.content) : {};
  jsonResponse(res, 200, { ok: true, ...summarizeManuscripts(data), sha: file.sha });
}

async function handleRun(req, res) {
  const body = await readJson(req);
  const token = String(body.githubToken || "").trim();
  const { owner, repo } = repoFromSettings(body.settings || {});
  if (!token) {
    jsonResponse(res, 400, { ok: false, errors: ["需要 GitHub Token 才能触发检查"] });
    return;
  }
  const mode = body.mode === "test" ? "test" : "normal";
  const result = await dispatchWorkflow({ owner, repo, token, workflow: DEFAULT_WORKFLOW, ref: DEFAULT_REF, mode });
  jsonResponse(res, 200, { ok: true, mode, ...result });
}

async function handleUnarchive(req, res) {
  const body = await readJson(req);
  const token = String(body.githubToken || "").trim();
  const { owner, repo } = repoFromSettings(body.settings || {});
  if (!token) {
    jsonResponse(res, 400, { ok: false, errors: ["需要 GitHub Token 才能恢复监测"] });
    return;
  }
  const stateRepo = stateRepoCoordinates();
  const file = await getRepoFile({ ...stateRepo, token, path: "data/manuscripts.json", ref: DEFAULT_REF });
  if (!file.exists) {
    jsonResponse(res, 404, {
      ok: false,
      errors: [`状态仓 ${STATE_REPO} 里还没有 data/manuscripts.json`],
    });
    return;
  }
  const { data, restored } = unarchiveRecord(JSON.parse(file.content), body.needle);
  const commit = await putRepoFile({
    ...stateRepo,
    token,
    path: "data/manuscripts.json",
    content: JSON.stringify(data, null, 2) + "\n",
    sha: file.sha,
    message: "chore: 恢复稿件监测 [skip ci]",
    branch: DEFAULT_REF,
  });
  jsonResponse(res, 200, { ok: true, restored, commit: commit.commit });
}

async function deployCloudflare(token) {
  const env = { ...process.env, CLOUDFLARE_API_TOKEN: token };
  // --no-install keeps npx from downloading an unpinned wrangler while this
  // process holds a Cloudflare token.
  const { stdout, stderr } = await execFile("npx", ["--no-install", "wrangler", "deploy"], {
    cwd: path.join(ROOT_DIR, "cloudflare-scheduler"),
    env,
    timeout: 120000,
    windowsHide: true,
  });
  return { stdout, stderr };
}

async function route(req, res) {
  try {
    assertLocalRequest(req);
    if (req.method === "GET") {
      await serveStatic(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/preview") {
      await handlePreview(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/current") {
      await handleCurrent(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/accounts") {
      await handleAccounts(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/save") {
      await handleSave(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/delete-account") {
      await handleDeleteAccount(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/status") {
      await handleStatus(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/manuscripts") {
      await handleManuscripts(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/run") {
      await handleRun(req, res);
      return;
    }
    if (req.method === "POST" && req.url === "/api/unarchive") {
      await handleUnarchive(req, res);
      return;
    }
    textResponse(res, 404, "Not found");
  } catch (error) {
    const rejected = /Only local requests|Unexpected Host header/.test(error?.message || "");
    jsonResponse(res, rejected ? 403 : 500, {
      ok: false,
      errors: [userFacingError(error)],
    });
  }
}

const server = http.createServer(route);
server.listen(PORT, HOST, () => {
  console.log(`Local config UI is running at http://${HOST}:${PORT}`);
  console.log("Press Ctrl+C to stop.");
});
