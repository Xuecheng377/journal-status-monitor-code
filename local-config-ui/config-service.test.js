const assert = require("node:assert/strict");
const test = require("node:test");
const {
  beijingHourToUtcHour,
  buildCronExpressions,
  buildSecrets,
  assignPlatformAdditionSlots,
  listPlatformAccounts,
  KEEP_EXISTING_SECRET,
  mergeExistingSecretValues,
  parseWranglerToml,
  buildWranglerToml,
  buildEnvPreview,
  validateSettings,
} = require("./config-service");

function completeSettings() {
  return {
    repository: {
      owner: "Xuecheng377",
      name: "journal-status-monitor",
      workflow: "monitor.yml",
      ref: "main",
    },
    platforms: {
      ieee: {
        email: "author@example.com",
        password: "ieee-password",
        url: "https://mc.manuscriptcentral.com/example",
      },
      elsevier: {
        email: "",
        password: "",
        url: "",
      },
    },
    email: {
      sender: "sender@example.com",
      password: "smtp-secret",
      receivers: "receiver@example.com",
      smtpServer: "smtp.example.com",
      smtpPort: "465",
    },
    schedule: {
      weeklyReport: { dayOfWeek: 1, hour: 8, minute: 17 },
      normalChecks: { hours: [0, 4, 8, 12, 16, 20], minute: 17 },
    },
    runtime: {
      archiveTerminal: true,
      includeArchivedInReport: false,
      terminalStatusKeywords: "accept,accepted,published,rejected,withdrawn",
    },
  };
}

test("converts Beijing hours to UTC hours", () => {
  assert.equal(beijingHourToUtcHour(8), 0);
  assert.equal(beijingHourToUtcHour(22), 14);
});

test("builds one cron when the weekly report shares a normal check hour", () => {
  // 08:00 is both a normal check and the weekly window, so the Worker decides
  // the mode from the day + hour and no second cron is needed.
  assert.deepEqual(buildCronExpressions(completeSettings().schedule), [
    "17,27,37 0,4,8,12,16,20 * * *",
  ]);
});

test("adds a weekly cron with a weekday name when the report hour is not a normal check", () => {
  const crons = buildCronExpressions({
    weeklyReport: { dayOfWeek: 1, hour: 9, minute: 17 },
    normalChecks: { hours: [0, 4, 8, 12, 16, 20], minute: 17 },
  });
  // Cloudflare weekdays are 1 = Sunday .. 7 = Saturday, so a bare "1" would run
  // on Sunday. The generator must emit a name instead.
  assert.equal(crons[0], "17,27,37 1 * * MON");
  assert.equal(crons[1], "17,27,37 0,4,8,12,16,20 * * *");
});

test("builds GitHub secrets without empty optional platform values", () => {
  const secrets = buildSecrets(completeSettings());
  assert.equal(secrets.IEEE_EMAIL, "author@example.com");
  assert.equal(secrets.IEEE_PASSWORD, "ieee-password");
  assert.equal(secrets.EMAIL_PASSWORD, "smtp-secret");
  assert.equal(secrets.ARCHIVE_TERMINAL, "true");
  assert.equal(secrets.INCLUDE_ARCHIVED_IN_REPORT, "false");
  assert.equal(Object.hasOwn(secrets, "ELSEVIER_PASSWORD"), false);
});

test("validates incomplete platform settings", () => {
  const settings = completeSettings();
  settings.platforms.ieee.password = "";
  const errors = validateSettings(settings);
  assert.equal(errors.some((error) => error.includes("IEEE 配置不完整")), true);
});

test("builds wrangler toml with repository variables and crons", () => {
  const toml = buildWranglerToml(completeSettings());
  assert.match(toml, /GITHUB_OWNER = "Xuecheng377"/);
  assert.match(toml, /GITHUB_REPO = "journal-status-monitor"/);
  assert.match(toml, /"17,27,37 0,4,8,12,16,20 \* \* \*"/);
  assert.doesNotMatch(toml, /\* \* 1"/);
});

test("writes the schedule into vars so the Worker cannot disagree with the cron", () => {
  const settings = completeSettings();
  settings.schedule = {
    weeklyReport: { dayOfWeek: 3, hour: 9, minute: 5 },
    normalChecks: { hours: [21, 9, 13], minute: 5 },
  };
  const toml = buildWranglerToml(settings);
  assert.match(toml, /DAILY_REPORT_HOUR = "9"/);
  assert.match(toml, /WEEKLY_REPORT_DAY = "3"/);
  assert.match(toml, /NORMAL_HOURS = "9,13,21"/);
  assert.match(toml, /WINDOW_MINUTE = "5"/);
  assert.match(toml, /WINDOW_TOLERANCE_MINUTES = "20"/);

  // Round trip: what the UI writes must be what the UI reads back.
  const parsed = parseWranglerToml(toml);
  assert.equal(parsed.schedule.weeklyReport.hour, 9);
  assert.equal(parsed.schedule.weeklyReport.dayOfWeek, 3);
  assert.deepEqual(parsed.schedule.normalChecks.hours, [9, 13, 21]);
  assert.equal(parsed.schedule.weeklyReport.minute, 5);
});

test("env preview never echoes a secret value", () => {
  const preview = buildEnvPreview(completeSettings());
  assert.match(preview, /IEEE_EMAIL=author@example\.com/);
  assert.match(preview, /IEEE_PASSWORD=\*+/);
  assert.match(preview, /EMAIL_PASSWORD=\*+/);
  assert.doesNotMatch(preview, /ieee-password/);
  assert.doesNotMatch(preview, /smtp-secret/);
});

test("keeps existing secret values when form field is blank", () => {
  const settings = completeSettings();
  settings.platforms.ieee.password = "";
  settings.email.password = "";
  const merged = mergeExistingSecretValues(settings, {
    IEEE_PASSWORD: true,
    EMAIL_PASSWORD: true,
  });
  assert.equal(merged.IEEE_PASSWORD, "__KEEP_EXISTING_SECRET__");
  assert.equal(merged.EMAIL_PASSWORD, "__KEEP_EXISTING_SECRET__");
  assert.equal(Object.hasOwn(merged, "IEEE_EMAIL"), true);
});

test("adds a new platform account to the next empty slot without replacing existing account secrets", () => {
  const settings = completeSettings();
  delete settings.platforms;
  settings.platformAccounts = {
    additions: [
      {
        platform: "ieee",
        email: "second-author@example.com",
        password: "second-password",
        url: "https://mc.manuscriptcentral.com/second",
      },
    ],
  };

  const merged = mergeExistingSecretValues(settings, {
    IEEE_EMAIL: true,
    IEEE_PASSWORD: true,
    IEEE_URL: true,
  });

  assert.equal(merged.IEEE_EMAIL, KEEP_EXISTING_SECRET);
  assert.equal(merged.IEEE_PASSWORD, KEEP_EXISTING_SECRET);
  assert.equal(merged.IEEE_URL, KEEP_EXISTING_SECRET);
  assert.equal(merged.IEEE_2_EMAIL, "second-author@example.com");
  assert.equal(merged.IEEE_2_PASSWORD, "second-password");
  assert.equal(merged.IEEE_2_URL, "https://mc.manuscriptcentral.com/second");
});

test("new platform accounts start from slot 2 even when existing secrets were not checked", () => {
  const settings = completeSettings();
  delete settings.platforms;
  settings.platformAccounts = {
    additions: [
      {
        platform: "ieee",
        email: "new-author@example.com",
        password: "new-password",
        url: "https://mc.manuscriptcentral.com/new",
      },
    ],
  };

  const secrets = buildSecrets(settings);

  assert.equal(Object.hasOwn(secrets, "IEEE_EMAIL"), false);
  assert.equal(secrets.IEEE_2_EMAIL, "new-author@example.com");
  assert.equal(secrets.IEEE_2_PASSWORD, "new-password");
  assert.equal(secrets.IEEE_2_URL, "https://mc.manuscriptcentral.com/new");
});

test("uses explicit slot for retrying the same added platform account", () => {
  const settings = completeSettings();
  delete settings.platforms;
  settings.platformAccounts = {
    additions: [
      {
        platform: "ieee",
        slot: 2,
        email: "new-author@example.com",
        password: "new-password",
        url: "https://mc.manuscriptcentral.com/new",
      },
    ],
  };

  const secrets = buildSecrets(settings, {
    existingSecrets: {
      IEEE_2_EMAIL: true,
      IEEE_2_PASSWORD: true,
      IEEE_2_URL: true,
    },
  });

  assert.equal(secrets.IEEE_2_EMAIL, "new-author@example.com");
  assert.equal(Object.hasOwn(secrets, "IEEE_3_EMAIL"), false);
});

test("records and reuses platform addition slot assignments", () => {
  const settings = completeSettings();
  delete settings.platforms;
  settings.platformAccounts = {
    additions: [
      {
        id: "addition-1",
        platform: "ieee",
        email: "new-author@example.com",
        password: "new-password",
        url: "https://mc.manuscriptcentral.com/new",
      },
    ],
  };

  const first = assignPlatformAdditionSlots(settings, {}, {});
  assert.equal(first.settings.platformAccounts.additions[0].slot, 2);
  assert.deepEqual(first.assignments["addition-1"], { platform: "ieee", slot: 2 });

  const retry = assignPlatformAdditionSlots(settings, {
    IEEE_2_EMAIL: true,
    IEEE_2_PASSWORD: true,
    IEEE_2_URL: true,
  }, first.assignments);

  assert.equal(retry.settings.platformAccounts.additions[0].slot, 2);
});

test("lists configured platform account slots from GitHub secrets and local metadata", () => {
  const accounts = listPlatformAccounts(
    {
      IEEE_EMAIL: true,
      IEEE_PASSWORD: true,
      IEEE_URL: true,
      IEEE_2_EMAIL: true,
      IEEE_2_PASSWORD: true,
      IEEE_2_URL: true,
      ELSEVIER_2_EMAIL: true,
    },
    {
      IEEE_2: {
        email: "second@example.com",
        url: "https://mc.manuscriptcentral.com/second",
      },
    },
  );

  assert.deepEqual(
    accounts.map((account) => ({
      label: account.label,
      complete: account.complete,
      email: account.email,
      url: account.url,
    })),
    [
      { label: "IEEE", complete: true, email: "", url: "" },
      { label: "IEEE_2", complete: true, email: "second@example.com", url: "https://mc.manuscriptcentral.com/second" },
      { label: "ELSEVIER_2", complete: false, email: "", url: "" },
    ],
  );
});

test("validates blank secret fields when an existing secret is present", () => {
  const settings = completeSettings();
  settings.platforms.ieee.password = "";
  settings.email.password = "";
  const errors = validateSettings(settings, {
    existingSecrets: {
      IEEE_PASSWORD: true,
      EMAIL_PASSWORD: true,
    },
  });
  assert.deepEqual(errors, []);
});

test("parses current wrangler toml schedule into form settings", () => {
  const parsed = parseWranglerToml(`name = "journal-status-monitor-scheduler"
[vars]
GITHUB_OWNER = "Xuecheng377"
GITHUB_REPO = "journal-status-monitor"
GITHUB_WORKFLOW = "monitor.yml"
GITHUB_REF = "main"
[triggers]
crons = [
  "17,27,37 0 * * 1",
  "17,27,37 0,4,8,12,16,20 * * *"
]`);
  assert.equal(parsed.repository.owner, "Xuecheng377");
  assert.equal(parsed.schedule.weeklyReport.dayOfWeek, 1);
  assert.equal(parsed.schedule.weeklyReport.hour, 8);
  assert.deepEqual(parsed.schedule.normalChecks.hours, [0, 4, 8, 12, 16, 20]);
});
