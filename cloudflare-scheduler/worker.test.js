import assert from "node:assert/strict";
import test from "node:test";

import { resolveWindow, scheduleConfig } from "./worker.js";

// A Beijing wall-clock time expressed as a UTC instant.
function beijing(year, month, day, hour, minute) {
  return new Date(Date.UTC(year, month - 1, day, hour - 8, minute, 0));
}

test("fires on the configured minute inside a normal hour", () => {
  const window = resolveWindow(beijing(2026, 10, 21, 12, 17), {});
  assert.equal(window?.mode, "normal");
  assert.equal(window?.key, "2026-10-21T12");
  assert.equal(window?.label, "2026-10-21 12:17");
  assert.equal(window?.minutesLate, 0);
});

test("a delayed cron still lands in the same window instead of being dropped", () => {
  for (const minute of [18, 21, 26]) {
    const window = resolveWindow(beijing(2026, 10, 21, 16, minute), {});
    assert.equal(window?.mode, "normal", `minute ${minute}`);
    assert.equal(window?.key, "2026-10-21T16", `minute ${minute}`);
  }
});

test("fallback minutes dedupe to the same window key", () => {
  const onTime = resolveWindow(beijing(2026, 10, 21, 20, 17), {});
  const fallback = resolveWindow(beijing(2026, 10, 21, 20, 27), {});
  assert.equal(onTime?.key, fallback?.key);
});

test("every fallback minute in the cron lands in the window", () => {
  // The cron fires at :17, :27 and :37. With a 10 minute tolerance the :37
  // wake-up was outside the window and silently dropped, so the documented
  // third fallback never existed.
  for (const minute of [17, 27, 37]) {
    const window = resolveWindow(beijing(2026, 10, 21, 12, minute), {});
    assert.equal(window?.mode, "normal", `minute ${minute}`);
    assert.equal(window?.key, "2026-10-21T12", `minute ${minute}`);
  }
});

test("ignore minutes outside the tolerance", () => {
  assert.equal(resolveWindow(beijing(2026, 10, 21, 12, 16), {}), null);
  assert.equal(resolveWindow(beijing(2026, 10, 21, 12, 38), {}), null);
  assert.equal(resolveWindow(beijing(2026, 10, 21, 12, 0), {}), null);
});

test("hours outside the configured set are not triggered", () => {
  assert.equal(resolveWindow(beijing(2026, 10, 21, 9, 17), {}), null);
  assert.equal(resolveWindow(beijing(2026, 10, 21, 23, 17), {}), null);
});

test("Monday 08:xx is the weekly report, other days are normal checks", () => {
  // 2026-10-19 is a Monday.
  assert.equal(resolveWindow(beijing(2026, 10, 19, 8, 17), {})?.mode, "daily_report");
  assert.equal(resolveWindow(beijing(2026, 10, 20, 8, 17), {})?.mode, "normal");
});

test("schedule comes from vars, so cron and trigger logic cannot drift", () => {
  const env = {
    DAILY_REPORT_HOUR: "9",
    WEEKLY_REPORT_DAY: "3",
    NORMAL_HOURS: "9,13,21",
    WINDOW_MINUTE: "5",
    WINDOW_TOLERANCE_MINUTES: "4",
  };
  const config = scheduleConfig(env);
  assert.equal(config.reportHour, 9);
  assert.equal(config.reportDay, 3);
  assert.deepEqual(Array.from(config.normalHours).sort((a, b) => a - b), [9, 13, 21]);

  const wednesdayNine = beijing(2026, 10, 21, 9, 5);
  assert.equal(resolveWindow(wednesdayNine, env)?.mode, "daily_report");
  assert.equal(resolveWindow(beijing(2026, 10, 21, 13, 7), env)?.mode, "normal");
  assert.equal(resolveWindow(beijing(2026, 10, 21, 13, 10), env), null);
  assert.equal(resolveWindow(beijing(2026, 10, 21, 11, 17), env), null);
});

test("invalid vars fall back to the documented defaults", () => {
  const config = scheduleConfig({ NORMAL_HOURS: "not,hours", WINDOW_MINUTE: "abc" });
  assert.equal(config.windowMinute, 17);
  assert.deepEqual(Array.from(config.normalHours).sort((a, b) => a - b), [0, 4, 8, 12, 16, 20]);
});