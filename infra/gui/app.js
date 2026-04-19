(function () {
  "use strict";

  const API = (window.BOT_API_URL || "").replace(/\/+$/, "");
  const $ = (id) => document.getElementById(id);

  async function call(path, opts = {}) {
    const url = API + path;
    const resp = await fetch(url, {
      headers: { "content-type": "application/json" },
      ...opts,
    });
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch {
      return { raw: text };
    }
  }

  function setChip(el, label, onCondition) {
    el.textContent = label;
    el.classList.remove("on", "off");
    if (onCondition === true) el.classList.add("on");
    else if (onCondition === false) el.classList.add("off");
  }

  function fmtTs(v) {
    if (!v) return "-";
    return String(v).replace("T", " ").replace("Z", "");
  }

  async function loadStatus() {
    const s = await call("/status");
    const hb = s.heartbeat || {};
    setChip($("heartbeat-chip"), "heartbeat: " + (hb.status || "none"), !!hb.status);
    setChip(
      $("dry-run-chip"),
      "dry_run: " + (hb.dry_run === undefined ? "?" : hb.dry_run),
      hb.dry_run !== false,
    );
    setChip(
      $("kill-chip"),
      "kill_switch: " + (hb.kill_switch === undefined ? "?" : hb.kill_switch),
      hb.kill_switch !== true,
    );

    const tbody = document.querySelector("#positions-table tbody");
    tbody.innerHTML = "";
    (s.positions || []).forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${p.token_id || ""}</td>` +
        `<td>${p.size ?? ""}</td>` +
        `<td>${p.avg_price ?? ""}</td>` +
        `<td>${fmtTs(p.updated_at)}</td>`;
      tbody.appendChild(tr);
    });

    const ul = $("errors-list");
    ul.innerHTML = "";
    (s.recent_errors || []).forEach((e) => {
      const li = document.createElement("li");
      li.textContent = `${fmtTs(e.sk && e.sk.replace("error#", ""))} [${e.where || ""}] ${e.message || ""}`;
      ul.appendChild(li);
    });
  }

  async function loadConfig() {
    const cfg = await call("/config");
    $("config-json").value = JSON.stringify(cfg, null, 2);
  }

  async function loadSignals() {
    const r = await call("/signals?limit=50");
    const tbody = document.querySelector("#signals-table tbody");
    tbody.innerHTML = "";
    (r.items || []).forEach((s) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${fmtTs(s.ts)}</td>` +
        `<td>${(s.question || "").slice(0, 50)}</td>` +
        `<td class="signal-${s.signal}">${s.signal || ""}</td>` +
        `<td>${s.last_price ?? ""}</td>` +
        `<td>${s.moving_average ?? ""}</td>` +
        `<td>${s.reason || ""}</td>`;
      tbody.appendChild(tr);
    });
  }

  async function loadOrders() {
    const r = await call("/orders?limit=50");
    const tbody = document.querySelector("#orders-table tbody");
    tbody.innerHTML = "";
    (r.items || []).forEach((o) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${fmtTs(o.ts)}</td>` +
        `<td>${o.side || ""}</td>` +
        `<td>${(o.token_id || "").slice(0, 20)}</td>` +
        `<td>${o.size ?? ""}</td>` +
        `<td>${o.price ?? ""}</td>` +
        `<td>${o.dry_run}</td>` +
        `<td>${o.ok}</td>` +
        `<td>${o.error || ""}</td>`;
      tbody.appendChild(tr);
    });
  }

  async function refreshAll() {
    await Promise.all([loadStatus(), loadConfig(), loadSignals(), loadOrders()]);
  }

  $("refresh").addEventListener("click", refreshAll);

  $("toggle-kill").addEventListener("click", async () => {
    const cfg = await call("/config");
    const next = !cfg.kill_switch;
    if (!confirm(`Set kill_switch to ${next}?`)) return;
    await call("/kill-switch", {
      method: "POST",
      body: JSON.stringify({ enabled: next }),
    });
    await refreshAll();
  });

  $("toggle-dry").addEventListener("click", async () => {
    const cfg = await call("/config");
    const next = !(cfg.dry_run !== false);
    if (!confirm(`Set dry_run to ${next}?`)) return;
    await call("/config", {
      method: "POST",
      body: JSON.stringify({ dry_run: next }),
    });
    await refreshAll();
  });

  $("save-config").addEventListener("click", async () => {
    let body;
    try {
      body = JSON.parse($("config-json").value);
    } catch (e) {
      alert("Invalid JSON: " + e.message);
      return;
    }
    delete body.bot_id;
    delete body.updated_at;
    const r = await call("/config", { method: "POST", body: JSON.stringify(body) });
    if (r.error) alert("Error: " + r.error);
    await refreshAll();
  });

  if (!API) {
    document.body.insertAdjacentHTML(
      "afterbegin",
      "<p style='background:#f85149;color:#fff;padding:1rem;text-align:center'>" +
      "BOT_API_URL is not configured. Deploy via CDK or edit config.js." +
      "</p>",
    );
  } else {
    refreshAll().catch((e) => console.error(e));
    setInterval(refreshAll, 15000);
  }
})();
