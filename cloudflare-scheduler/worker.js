const DEFAULT_OWNER = "Xuecheng377";
const DEFAULT_REPO = "journal-status-monitor";
const DEFAULT_WORKFLOW = "monitor.yml";
const DEFAULT_REF = "main";

// Schedule defaults. Every value can be overridden by the matching [vars] entry
// written by local-config-ui, so the cron expressions and the trigger logic can
// never drift apart again.
const DEFAULT_REPORT_HOUR = 8; // Beijing hour for the weekly report
const DEFAULT_REPORT_DAY = 1; // ISO weekday: 1 = Monday
const DEFAULT_NORMAL_HOURS = "0,4,8,12,16,20"; // Beijing hours: one check every 4 hours
const DEFAULT_WINDOW_MINUTE = 17; // first minute of a trigger window
const DEFAULT_WINDOW_TOLERANCE_MINUTES = 20; // covers the :17 SQL + :27/:37 fallbacks
const DEFAULT_LOOKBACK_MINUTES = 45;

function beijingTime(date = new Date()) {
  return new Date(date.getTime() + 8 * 60 * 60 * 1000);
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function formatBeijingTime(date = new Date()) {
  const bj = beijingTime(date);
  return `${bj.getUTCFullYear()}-${pad(bj.getUTCMonth() + 1)}-${pad(bj.getUTCDate())} ${pad(
    bj.getUTCHours(),
  )}:${pad(bj.getUTCMinutes())}:${pad(bj.getUTCSeconds())}`;
}

function toInt(value, fallback) {
  if (value === undefined || value === null || value === "") {
    return fallback;
  }
  const parsed = Number.parseInt(String(value), 10);
  return Number.isInteger(parsed) ? parsed : fallback;
}

function parseHourList(value, fallback) {
  const text = value === undefined || value === null || value === "" ? fallback : value;
  const hours = String(text)
    .split(/[,\s]+/)
    .map((item) => Number.parseInt(item, 10))
    .filter((hour) => Number.isInteger(hour) && hour >= 0 && hour <= 23);
  return new Set(hours.length ? hours : fallback.split(",").map((item) => Number.parseInt(item, 10)));
}

function scheduleConfig(env = {}) {
  return {
    reportHour: toInt(env.DAILY_REPORT_HOUR, DEFAULT_REPORT_HOUR),
    reportDay: toInt(env.WEEKLY_REPORT_DAY, DEFAULT_REPORT_DAY),
    normalHours: parseHourList(env.NORMAL_HOURS, DEFAULT_NORMAL_HOURS),
    windowMinute: toInt(env.WINDOW_MINUTE, DEFAULT_WINDOW_MINUTE),
    tolerance: toInt(env.WINDOW_TOLERANCE_MINUTES, DEFAULT_WINDOW_TOLERANCE_MINUTES),
    lookbackMinutes: toInt(env.LOOKBACK_MINUTES, DEFAULT_LOOKBACK_MINUTES),
  };
}

function resolveWindow(date = new Date(), env = {}) {
  const config = scheduleConfig(env);
  const bj = beijingTime(date);
  const day = bj.getUTCDay();
  const hour = bj.getUTCHours();
  const minute = bj.getUTCMinutes();

  // Cloudflare fires exactly on the cron minute, but wake-ups can arrive a few
  // minutes late. Accept the whole window instead of demanding minute === 17,
  // which used to drop the window silently.
  const offset = minute - config.windowMinute;
  if (offset < 0 || offset > config.tolerance) {
    return null;
  }

  let mode;
  if (day === config.reportDay && hour === config.reportHour) {
    mode = "daily_report";
  } else if (config.normalHours.has(hour)) {
    mode = "normal";
  } else {
    return null;
  }

  const stamp = `${bj.getUTCFullYear()}-${pad(bj.getUTCMonth() + 1)}-${pad(bj.getUTCDate())}`;
  return {
    mode,
    key: `${stamp}T${pad(hour)}`,
    label: `${stamp} ${pad(hour)}:${pad(config.windowMinute)}`,
    minutesLate: offset,
  };
}

function githubHeaders(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": "journal-status-external-scheduler",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

async function getWorkflowRuns(env, token) {
  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repo = env.GITHUB_REPO || DEFAULT_REPO;
  const workflow = env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW;
  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/runs?event=workflow_dispatch&per_page=20`,
    {
      headers: githubHeaders(token),
    },
  );

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub run lookup failed: ${response.status} ${body}`);
  }

  return response.json();
}

async function alreadyDispatchedForWindow(env, token, window) {
  const runs = await getWorkflowRuns(env, token);
  const threshold = Date.now() - scheduleConfig(env).lookbackMinutes * 60 * 1000;
  return (runs.workflow_runs || []).some((run) => {
    const createdAt = Date.parse(run.created_at || "");
    if (!Number.isFinite(createdAt) || createdAt < threshold) {
      return false;
    }
    const title = String(run.display_title || "");
    const sameWindow = title.includes(`window=${window.key}`) || title.includes(`window=${window.label}`);
    if (!sameWindow) {
      return false;
    }
    // A queued or successful run already covers this window; a failed one is
    // worth retrying on the fallback minute.
    return run.status !== "completed" || run.conclusion !== "failure";
  });
}

async function dispatchWorkflow(env, window) {
  const owner = env.GITHUB_OWNER || DEFAULT_OWNER;
  const repo = env.GITHUB_REPO || DEFAULT_REPO;
  const workflow = env.GITHUB_WORKFLOW || DEFAULT_WORKFLOW;
  const ref = env.GITHUB_REF || DEFAULT_REF;
  const token = env.GITHUB_TOKEN;

  if (!token) {
    throw new Error("Missing GITHUB_TOKEN secret.");
  }

  if (await alreadyDispatchedForWindow(env, token, window)) {
    console.log(`Skipped duplicate fallback for Beijing window ${window.label}.`);
    return "skipped";
  }

  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        ...githubHeaders(token),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref,
        inputs: {
          mode: window.mode,
          window: window.key,
        },
      }),
    },
  );

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub dispatch failed: ${response.status} ${body}`);
  }
  return "triggered";
}

export default {
  async scheduled(event, env, ctx) {
    const scheduledAt = event.scheduledTime ? new Date(event.scheduledTime) : new Date();
    const window = resolveWindow(scheduledAt, env);
    if (!window) {
      console.log("Outside configured Beijing trigger windows; skipped.");
      return;
    }

    ctx.waitUntil(
      dispatchWorkflow(env, window).then((result) => {
        console.log(
          `${result} GitHub workflow_dispatch with mode=${window.mode}, window=${window.key}, ` +
            `${window.minutesLate} minute(s) into the window.`,
        );
      }),
    );
  },

  async fetch(request, env) {
    const config = scheduleConfig(env);
    const window = resolveWindow(new Date(), env);
    return Response.json({
      ok: true,
      beijingTime: formatBeijingTime(),
      modeIfScheduledNow: window?.mode || null,
      windowIfScheduledNow: window?.key || null,
      schedule: {
        reportDay: config.reportDay,
        reportHour: config.reportHour,
        normalHours: Array.from(config.normalHours).sort((a, b) => a - b),
        windowMinute: config.windowMinute,
        windowToleranceMinutes: config.tolerance,
      },
    });
  },
};

// Exported for the node test suite; the Worker runtime only uses default.
export { resolveWindow, scheduleConfig, beijingTime };