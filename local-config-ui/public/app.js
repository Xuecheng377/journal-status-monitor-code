/**
 * Status and manuscript views for the local config UI.
 *
 * Deliberately separate from main.js: that file owns the settings form, this one
 * owns the read-only side ("is it running?", "what is being watched?"). Both talk
 * to the same 127.0.0.1 server and reuse the same GitHub token field.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const TAB_TITLES = {
    status: ["运行状态", "先填一次 GitHub Token，就能看到监控是否真的在跑。"],
    manuscripts: ["稿件清单", "在监控的稿件、已归档的稿件，以及当前判定规则。"],
    platforms: ["投稿账号", "每个账号对应一组 GitHub Secret，密码只能覆盖、不能读出。"],
    email: ["邮件通知", "新稿件、状态变化、失败告警和每周报告都会发到这个邮箱。"],
    schedule: ["检查时间", "决定 Cloudflare Worker 什么时候唤醒抓取。"],
    runtime: ["高级设置", "改错会导致漏报或重复报警，保持默认通常更好。"],
    deploy: ["保存与部署", "把配置写入 GitHub Secrets，并（可选）部署 Worker。"],
  };
  const state = { status: null, loaded: false };

  function showTab(name) {
    const target = TAB_TITLES[name] ? name : "status";
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === target);
    });
    document.querySelectorAll("nav a[data-tab]").forEach((link) => {
      link.classList.toggle("active", link.dataset.tab === target);
    });
    if (TAB_TITLES[target]) {
      $("pageTitle").textContent = TAB_TITLES[target][0];
      $("pageHint").textContent = TAB_TITLES[target][1];
    }
    if (location.hash.slice(1) !== target) {
      history.replaceState(null, "", `#${target}`);
    }
  }

  function token() {
    return ($("githubToken")?.value || "").trim();
  }

  async function post(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({ ok: false, errors: ["服务器返回了无法解析的内容"] }));
    if (!response.ok || data.ok === false) {
      const message = (data.errors || ["请求失败"]).join("；");
      throw new Error(message);
    }
    return data;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function setHealth(kind, headline, detail) {
    const banner = $("healthBanner");
    banner.className = `health health-${kind}`;
    $("healthHeadline").textContent = headline;
    $("healthDetail").textContent = detail || "";
  }

  function renderStatus(data) {
    state.status = data;
    state.loaded = true;
    const health = data.health || {};
    const counts = data.manuscripts?.counts || { active: 0, archived: 0 };
    const kind = health.state === "ok" ? "ok" : health.state === "warn" ? "warn" : "error";
    const parts = [];
    if (health.next?.label) parts.push(`下次 ${health.next.label}`);
    if (counts.active || counts.archived) parts.push(`在监控 ${counts.active} 篇 · 已归档 ${counts.archived} 篇`);
    if (health.consecutiveFailures > 1) parts.push(`连续失败 ${health.consecutiveFailures} 次`);
    if (data.runsError) parts.push(`读取运行记录失败：${data.runsError}`);
    if (data.dataError) parts.push(`读取稿件数据失败：${data.dataError}`);
    setHealth(kind, health.headline || "状态未知", parts.join(" · "));

    $("statusMeta").textContent = `${data.repo} · ${new Date().toLocaleTimeString("zh-CN")} 读取`;
    $("statLastSuccess").textContent = health.lastSuccess
      ? `${new Date(health.lastSuccess.createdAt).toLocaleString("zh-CN", { hour12: false })}`
      : "还没有成功记录";
    $("statToday").textContent = `已跑 ${health.done ?? 0} 次 · 今日计划 ${health.planned ?? 0} 次（已到点 ${
      health.due ?? 0
    }）`;
    $("statNext").textContent = health.next?.label || "未知";
    $("statCounts").textContent = `${counts.active} / ${counts.archived}`;

    const list = $("runList");
    if (!data.runs.length) {
      list.innerHTML = '<p class="empty">还没有运行记录。检查 Cloudflare Worker 是否部署、GitHub Token 是否过期。</p>';
    } else {
      list.innerHTML = data.runs
        .map((run) => {
          const badge = run.status !== "completed" ? "run" : run.conclusion === "success" ? "ok" : "bad";
          const label = run.status !== "completed" ? "运行中" : run.conclusion === "success" ? "成功" : "失败";
          const when = new Date(run.createdAt).toLocaleString("zh-CN", { hour12: false });
          return `<a class="run ${badge}" href="${escapeHtml(run.url)}" target="_blank" rel="noreferrer">
            <span class="run-badge">${label}</span>
            <span class="run-when">${escapeHtml(when)}</span>
            <span class="run-mode">${run.manual ? "手动" : "自动"}</span>
            <span class="run-title">${escapeHtml(run.title || "")}</span>
          </a>`;
        })
        .join("");
    }
    renderManuscripts(data.manuscripts);
  }

  function manuscriptRow(record, allowRestore) {
    const status = escapeHtml(record.status || record.lastSeenStatus || "—");
    const stale = record.stale ? '<span class="tag warn">状态未解析</span>' : "";
    const archived = record.archived
      ? `<span class="tag">归档于 ${escapeHtml(record.archivedAt || "")}</span>`
      : "";
    const action = allowRestore
      ? `<button type="button" class="link-btn" data-unarchive="${escapeHtml(record.id || record.title)}">恢复监测</button>`
      : "";
    const link = record.url
      ? `<a href="${escapeHtml(record.url)}" target="_blank" rel="noreferrer">打开投稿系统</a>`
      : "";
    return `<div class="manuscript">
      <div class="manuscript-head">
        <span class="ms-id">${escapeHtml(record.id || "无编号")}</span>
        <span class="ms-status">${status}</span>
        ${stale}${archived}
      </div>
      <div class="ms-title">${escapeHtml(record.title || "")}</div>
      <div class="ms-meta">
        <span>首次记录 ${escapeHtml(record.firstSeen || "—")}</span>
        <span>最近检查 ${escapeHtml(record.lastChecked || "—")}</span>
        ${link}${action}
      </div>
    </div>`;
  }

  function renderManuscripts(data) {
    const summary = data || { active: [], archived: [], counts: {} };
    $("manuscriptMeta").textContent = `共 ${summary.counts?.total ?? 0} 篇`;
    $("activeList").innerHTML = summary.active.length
      ? summary.active.map((record) => manuscriptRow(record, false)).join("")
      : '<p class="empty">没有在监控的稿件。</p>';
    $("archivedList").innerHTML = summary.archived.length
      ? summary.archived.map((record) => manuscriptRow(record, true)).join("")
      : '<p class="empty">没有已归档的稿件。</p>';
  }

  async function refreshStatus() {
    if (!token()) {
      setHealth("idle", "还没有填 GitHub Token", "Token 存在本页内存里，用来读取 Actions 运行记录和稿件数据。");
      showTab("deploy");
      return;
    }
    setHealth("idle", "正在读取运行状态…", "");
    try {
      const data = await post("/api/status", { githubToken: token() });
      renderStatus(data);
    } catch (error) {
      setHealth("error", "读取状态失败", error.message);
    }
  }

  async function runNow() {
    if (!token()) {
      setHealth("idle", "还没有填 GitHub Token", "先在上面填入 Token 再触发检查。");
      return;
    }
    $("runNowBtn").disabled = true;
    $("runNowHint").textContent = "已触发，正在等待 Cloudflare/GitHub 排队…";
    try {
      const result = await post("/api/run", { githubToken: token(), mode: "normal" });
      $("runNowHint").textContent = "已触发一次检查，约 1-3 分钟后刷新状态即可看到结果。";
      setTimeout(refreshStatus, 8000);
      return result;
    } catch (error) {
      $("runNowHint").textContent = `触发失败：${error.message}`;
    } finally {
      $("runNowBtn").disabled = false;
    }
  }

  let restoring = false;

  async function restore(needle, button) {
    if (!token()) {
      setHealth("idle", "还没有填 GitHub Token", "恢复监测需要写权限。");
      return;
    }
    // The file write is a read-modify-write on GitHub, so a second click
    // before the first finishes would restore twice or fail with a conflict.
    if (restoring) {
      return;
    }
    restoring = true;
    const label = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "恢复中...";
    }
    try {
      const result = await post("/api/unarchive", { githubToken: token(), needle });
      setHealth("ok", `已恢复 ${result.restored.join("、")} 的监测`, "下一次检查会重新跟踪这篇稿件。");
      await refreshStatus();
    } catch (error) {
      setHealth("error", "恢复失败", error.message);
    } finally {
      restoring = false;
      if (button) {
        button.disabled = false;
        button.textContent = label;
      }
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("nav a[data-tab]");
    if (link) {
      event.preventDefault();
      showTab(link.dataset.tab);
      return;
    }
    const restoreBtn = event.target.closest("[data-unarchive]");
    if (restoreBtn) {
      restore(restoreBtn.dataset.unarchive, restoreBtn);
    }
  });

  $("refreshStatusBtn").addEventListener("click", refreshStatus);
  $("runNowBtn").addEventListener("click", runNow);
  $("githubToken").addEventListener("change", () => {
    if (token() && !state.loaded) refreshStatus();
  });

  showTab(location.hash.slice(1) || "status");
  if (token()) refreshStatus();
})();