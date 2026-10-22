/**
 * Read-only status helpers for the local config UI.
 *
 * Everything here is pure: it takes the workflow runs (as returned by the GitHub
 * API) plus the schedule read from wrangler.toml and answers the three questions
 * the owner actually has - "is it running?", "when is the next check?" and "what
 * did the last one do?". Keeping it pure makes it testable without a token.
 */

const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;

function toDate(value) {
  if (value instanceof Date) {
    return value;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** Beijing calendar day, e.g. "2026-10-21". */
function dayKey(value) {
  const date = toDate(value);
  if (!date) {
    return "";
  }
  return new Date(date.getTime() + BEIJING_OFFSET_MS).toISOString().slice(0, 10);
}

/** Beijing wall clock, e.g. "22:17". */
function beijingClock(value) {
  const date = toDate(value);
  if (!date) {
    return "";
  }
  return new Date(date.getTime() + BEIJING_OFFSET_MS).toISOString().slice(11, 16);
}

function beijingWeekday(value) {
  const date = toDate(value);
  if (!date) {
    return 0;
  }
  return new Date(date.getTime() + BEIJING_OFFSET_MS).getUTCDay();
}

function parseHours(raw) {
  const text = Array.isArray(raw) ? raw.join(",") : String(raw ?? "");
  return text
    .split(",")
    .map((item) => Number.parseInt(item.trim(), 10))
    .filter((hour) => Number.isInteger(hour) && hour >= 0 && hour <= 23);
}

function parseMinute(raw, fallback = 17) {
  const minute = Number.parseInt(String(raw ?? ""), 10);
  return Number.isInteger(minute) && minute >= 0 && minute <= 59 ? minute : fallback;
}

/** Every scheduled wake-up for one Beijing day, as "HH:MM" strings. */
function plannedWindows(schedule = {}, day) {
  const hours = parseHours(schedule.NORMAL_HOURS ?? "0,4,8,12,16,20");
  const minute = parseMinute(schedule.WINDOW_MINUTE ?? 17);
  const weeklyDay = Number.parseInt(String(schedule.WEEKLY_REPORT_DAY ?? 1), 10);
  const weeklyHour = Number.parseInt(String(schedule.DAILY_REPORT_HOUR ?? 8), 10);
  const windows = hours.map((hour) => ({ hour, minute }));
  if (beijingWeekday(day) === weeklyDay && Number.isInteger(weeklyHour)) {
    windows.push({ hour: weeklyHour, minute });
  }
  // The weekly report may share an hour with a normal check: that window is the
  // weekly one, so count it once instead of listing the same minute twice.
  const seen = new Set();
  return windows
    .filter((window) => {
      const key = `${window.hour}:${window.minute}`;
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    })
    .sort((left, right) => left.hour * 60 + left.minute - (right.hour * 60 + right.minute))
    .map((window) => `${String(window.hour).padStart(2, "0")}:${String(window.minute).padStart(2, "0")}`);
}

function nextPlannedRun(schedule, now = new Date()) {
  const current = toDate(now) || new Date();
  const today = dayKey(current);
  const clock = beijingClock(current);
  for (const window of plannedWindows(schedule, current)) {
    if (window > clock) {
      return { label: `今天 ${window}`, clock: window, day: today };
    }
  }
  const tomorrow = new Date(current.getTime() + 24 * 60 * 60 * 1000);
  const first = plannedWindows(schedule, tomorrow)[0];
  return first ? { label: `明天 ${first}`, clock: first, day: dayKey(tomorrow) } : null;
}

function normalizeRun(run) {
  const title = String(run.title ?? run.display_title ?? "");
  return {
    id: run.id,
    number: run.number ?? run.run_number,
    title,
    status: String(run.status ?? ""),
    conclusion: run.conclusion ?? null,
    createdAt: run.createdAt ?? run.created_at,
    url: run.url ?? run.html_url,
    manual: /window=manual/i.test(title),
  };
}

function missingToday(schedule, runs, now = new Date()) {
  const current = toDate(now) || new Date();
  const clock = beijingClock(current);
  const windows = plannedWindows(schedule, current);
  const due = windows.filter((window) => window <= clock);
  const today = dayKey(current);
  const minute = String(parseMinute(schedule.WINDOW_MINUTE ?? 17)).padStart(2, "0");
  // Count covered windows, not raw runs: manual runs, fallback wake-ups and
  // duplicate dispatches must not make a skipped window look like it ran.
  const covered = new Set();
  for (const run of runs) {
    if (dayKey(run.createdAt) !== today || run.status !== "completed") {
      continue;
    }
    const match = /window=(\d{4}-\d{2}-\d{2})T(\d{2})/.exec(String(run.title ?? ""));
    if (!match) {
      continue;
    }
    covered.add(`${match[2]}:${minute}`);
  }
  const done = due.filter((window) => covered.has(window)).length;
  const failed = runs.filter(
    (run) => dayKey(run.createdAt) === today && run.status === "completed" && run.conclusion !== "success",
  ).length;
  return { due: due.length, planned: windows.length, done, failed, missing: Math.max(due.length - done, 0) };
}

function deriveHealth({ runs = [], schedule = {}, now = new Date() } = {}) {
  const normalized = runs.map(normalizeRun);
  const next = nextPlannedRun(schedule, now);
  const counts = missingToday(schedule, normalized, now);
  const lastRun = normalized[0] || null;
  let consecutiveFailures = 0;
  for (const run of normalized) {
    if (run.status !== "completed") {
      break;
    }
    if (run.conclusion === "success") {
      break;
    }
    consecutiveFailures += 1;
  }
  const lastSuccess = normalized.find((run) => run.conclusion === "success") || null;
  const base = { runs: normalized, lastRun, lastSuccess, next, ...counts };

  if (!lastRun) {
    return {
      ...base,
      state: "error",
      headline: "还没有任何运行记录，监控可能从未触发",
      consecutiveFailures: 0,
    };
  }
  if (lastRun.status !== "completed") {
    return {
      ...base,
      state: "warn",
      headline: `正在运行中（${lastRun.manual ? "手动" : "自动"}，${beijingClock(lastRun.createdAt)} 开始）`,
      consecutiveFailures,
    };
  }
  if (lastRun.conclusion !== "success") {
    const streak = consecutiveFailures > 1 ? `，已连续 ${consecutiveFailures} 次` : "";
    return {
      ...base,
      state: "error",
      headline: `最近一次检查失败（${beijingClock(lastRun.createdAt)}${lastRun.manual ? "，手动" : "，自动"}）${streak}`,
      consecutiveFailures,
    };
  }
  if (counts.missing > 0) {
    return {
      ...base,
      state: "warn",
      headline: `最近一次检查成功（${beijingClock(lastRun.createdAt)}），但今天少跑了 ${counts.missing} 次`,
      consecutiveFailures,
    };
  }
  return {
    ...base,
    state: "ok",
    headline: `最近一次检查成功（${beijingClock(lastRun.createdAt)}${lastRun.manual ? "，手动" : "，自动"}）`,
    consecutiveFailures,
  };
}

function summarizeManuscripts(data) {
  const records = Object.entries(data || {}).map(([key, record]) => ({
    key,
    id: record?.id || "",
    title: record?.title || "",
    status: record?.status || "",
    lastSeenStatus: record?.last_seen_status || "",
    source: record?.source || "",
    url: record?.url || "",
    firstSeen: record?.first_seen || "",
    lastChecked: record?.last_checked || "",
    archived: Boolean(record?.archived),
    archivedAt: record?.archived_at || "",
    suppressed: Boolean(record?.archive_suppressed),
    stale: Boolean(record?.stale),
  }));
  const active = records.filter((record) => !record.archived);
  const archived = records.filter((record) => record.archived);
  return {
    active: active.sort((left, right) => String(right.lastChecked).localeCompare(String(left.lastChecked))),
    archived: archived.sort((left, right) => String(right.archivedAt).localeCompare(String(left.archivedAt))),
    counts: { total: records.length, active: active.length, archived: archived.length },
  };
}

function unarchiveRecord(data, needle, now = new Date()) {
  const text = String(needle ?? "").trim().toLowerCase();
  if (!text) {
    throw new Error("需要稿件 ID 或标题片段");
  }
  const next = JSON.parse(JSON.stringify(data || {}));
  const restored = [];
  for (const [key, record] of Object.entries(next)) {
    if (!record || typeof record !== "object") {
      continue;
    }
    const haystack = `${record.id || ""} ${record.title || ""}`.toLowerCase();
    if (!haystack.includes(text) || !record.archived) {
      continue;
    }
    delete record.archived;
    delete record.archived_at;
    delete record.archive_reason;
    record.archive_suppressed = true;
    record.suppress_until_status = record.status || "";
    record.unarchived_at = now.toISOString().slice(0, 19).replace("T", " ");
    restored.push(record.id || key);
  }
  if (!restored.length) {
    throw new Error("没有匹配到已归档的稿件");
  }
  return { data: next, restored };
}

module.exports = {
  beijingClock,
  beijingWeekday,
  dayKey,
  deriveHealth,
  missingToday,
  nextPlannedRun,
  normalizeRun,
  parseHours,
  plannedWindows,
  summarizeManuscripts,
  unarchiveRecord,
};