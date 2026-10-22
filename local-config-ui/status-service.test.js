const assert = require("node:assert/strict");
const test = require("node:test");

const {
  beijingClock,
  dayKey,
  deriveHealth,
  nextPlannedRun,
  plannedWindows,
  summarizeManuscripts,
  unarchiveRecord,
} = require("./status-service");

const SCHEDULE = {
  NORMAL_HOURS: "0,4,8,12,16,20",
  WINDOW_MINUTE: 17,
  WEEKLY_REPORT_DAY: 1,
  DAILY_REPORT_HOUR: 8,
};

function run(overrides = {}) {
  return {
    id: 1,
    number: 1,
    title: "Journal Status Monitor (normal window=2026-10-21T22)",
    event: "workflow_dispatch",
    status: "completed",
    conclusion: "success",
    createdAt: "2026-10-21T14:17:10Z",
    url: "https://example.invalid/run/1",
    ...overrides,
  };
}

test("beijing helpers convert UTC to +08:00", () => {
  assert.equal(dayKey("2026-10-21T14:17:10Z"), "2026-10-21");
  assert.equal(beijingClock("2026-10-21T14:17:10Z"), "22:17");
  // 23:30 UTC is already the next day in Beijing.
  assert.equal(dayKey("2026-10-21T23:30:00Z"), "2026-10-22");
});

test("planned windows follow the wrangler schedule and add the weekly window", () => {
  const wednesday = new Date("2026-10-21T02:00:00Z");
  assert.deepEqual(plannedWindows(SCHEDULE, wednesday), [
    "00:17",
    "04:17",
    "08:17",
    "12:17",
    "16:17",
    "20:17",
  ]);
  const monday = new Date("2026-10-19T02:00:00Z");
  // The weekly report shares 08:17 with the normal check, so Monday has the
  // same six windows rather than a seventh duplicate entry.
  assert.deepEqual(plannedWindows(SCHEDULE, monday), [
    "00:17",
    "04:17",
    "08:17",
    "12:17",
    "16:17",
    "20:17",
  ]);
  assert.equal(plannedWindows(SCHEDULE, monday).filter((w) => w === "08:17").length, 1);
});

test("next planned run rolls over to tomorrow after the last window", () => {
  const late = new Date("2026-10-21T14:30:00Z"); // 22:30 Beijing
  assert.equal(nextPlannedRun(SCHEDULE, late).label, "明天 00:17");
  const early = new Date("2026-10-21T01:00:00Z"); // 09:00 Beijing
  assert.equal(nextPlannedRun(SCHEDULE, early).label, "今天 12:17");
});

test("healthy run with every window done reports ok", () => {
  const now = new Date("2026-10-21T14:30:00Z"); // 22:30 Beijing
  const runs = ["20:17", "16:17", "12:17", "08:17", "04:17", "00:17"].map((clock, index) =>
    run({
      id: index,
      title: `Journal Status Monitor (normal window=2026-10-21T${clock.slice(0, 2)})`,
      // Beijing wall clock on 2026-10-21 -> the matching UTC instant; Date.UTC
      // rolls a negative hour back into the previous UTC day on its own.
      createdAt: new Date(Date.UTC(2026, 9, 21, Number(clock.slice(0, 2)) - 8, 17, 0)).toISOString(),
    }),
  );
  const health = deriveHealth({ runs, schedule: SCHEDULE, now });
  assert.equal(health.state, "ok");
  assert.equal(health.done, 6);
  assert.equal(health.missing, 0);
  assert.match(health.headline, /最近一次检查成功/);
});

test("a failed last run is an error even when earlier runs succeeded", () => {
  const now = new Date("2026-10-21T14:30:00Z");
  const runs = [run({ id: 3, conclusion: "failure", createdAt: "2026-10-21T14:17:10Z" }), run({ id: 2 })];
  const health = deriveHealth({ runs, schedule: SCHEDULE, now });
  assert.equal(health.state, "error");
  assert.equal(health.consecutiveFailures, 1);
  assert.match(health.headline, /最近一次检查失败/);
});

test("consecutive failures are counted and surfaced", () => {
  const now = new Date("2026-10-21T14:30:00Z");
  const runs = [
    run({ id: 4, conclusion: "failure" }),
    run({ id: 3, conclusion: "failure" }),
    run({ id: 2, conclusion: "failure" }),
    run({ id: 1, conclusion: "success" }),
  ];
  const health = deriveHealth({ runs, schedule: SCHEDULE, now });
  assert.equal(health.consecutiveFailures, 3);
  assert.match(health.headline, /连续 3 次/);
});

test("manual runs do not count as covered windows", () => {
  const now = new Date("2026-10-21T14:30:00Z"); // 22:30, six windows due
  const runs = [run({ createdAt: "2026-10-21T12:17:10Z", title: "manual" })];
  const health = deriveHealth({ runs, schedule: SCHEDULE, now });
  assert.equal(health.state, "warn");
  // A hand-triggered run covers no scheduled window, so all six are missing.
  assert.equal(health.done, 0);
  assert.match(health.headline, /少跑了 6 次/);
});

test("fallback wake-ups in the same window count once", () => {
  const now = new Date("2026-10-21T14:30:00Z");
  const runs = [17, 27, 37].map((minute, index) =>
    run({
      id: index,
      title: "Journal Status Monitor (normal window=2026-10-21T00)",
      createdAt: `2026-10-20T16:${minute}:00Z`,
    }),
  );
  const health = deriveHealth({ runs, schedule: SCHEDULE, now });
  assert.equal(health.done, 1);
});

test("a queued run reports as in progress", () => {
  const health = deriveHealth({
    runs: [run({ id: 9, status: "in_progress", conclusion: null })],
    schedule: SCHEDULE,
    now: new Date("2026-10-21T14:30:00Z"),
  });
  assert.equal(health.state, "warn");
  assert.match(health.headline, /正在运行/);
});

test("no runs at all is an error, not a silent ok", () => {
  const health = deriveHealth({ runs: [], schedule: SCHEDULE, now: new Date("2026-10-21T14:30:00Z") });
  assert.equal(health.state, "error");
  assert.match(health.headline, /还没有任何运行记录/);
});

test("manuscripts are split into active and archived", () => {
  const summary = summarizeManuscripts({
    "a": { id: "26-TIE-3091", title: "A", status: "Under Review", archived: false },
    "b": { id: "26-TIE-3092", title: "B", status: "Accept", archived: true, archived_at: "2026-10-01 10:00:00" },
  });
  assert.equal(summary.counts.total, 2);
  assert.equal(summary.active.length, 1);
  assert.equal(summary.archived.length, 1);
  assert.equal(summary.archived[0].id, "26-TIE-3092");
});

test("unarchive clears the archive flags and records the suppression scope", () => {
  const data = {
    "x": {
      id: "26-TIE-3091",
      title: "Adaptive Control",
      status: "Reject with Possibility for Resubmission",
      archived: true,
      archived_at: "2026-07-17 10:00:00",
      archive_reason: "terminal_status",
    },
  };
  const result = unarchiveRecord(data, "26-tie-3091", new Date("2026-10-21T16:00:00Z"));
  const record = result.data.x;
  assert.deepEqual(result.restored, ["26-TIE-3091"]);
  assert.equal(record.archived, undefined);
  assert.equal(record.archive_reason, undefined);
  assert.equal(record.archive_suppressed, true);
  assert.equal(record.suppress_until_status, "Reject with Possibility for Resubmission");
  // The input must not be mutated.
  assert.equal(data.x.archived, true);
});

test("unarchive refuses an empty needle or an unknown manuscript", () => {
  assert.throws(() => unarchiveRecord({}, ""), /需要稿件 ID/);
  assert.throws(() => unarchiveRecord({ x: { id: "1", archived: true } }, "nope"), /没有匹配/);
});