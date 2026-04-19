(function () {
  "use strict";

  const CFG = window.BOT_CONFIG || {};
  const API = (CFG.apiUrl || window.BOT_API_URL || "").replace(/\/+$/, "");
  const $ = (id) => document.getElementById(id);

  // --- Auth state ----------------------------------------------------------
  // Tokens are held in memory for API calls. We persist only what is
  // required to survive a page reload within the same session:
  //   - id_token + access_token + expiry in sessionStorage (cleared on logout)
  //   - pkce verifier lives in sessionStorage only between redirect hops
  // We never store Polymarket wallet material here; the browser has no
  // access to Secrets Manager by design.
  const STORAGE = {
    ID_TOKEN: "pbm_id_token",
    ACCESS_TOKEN: "pbm_access_token",
    EXPIRES_AT: "pbm_expires_at",
    USER_EMAIL: "pbm_user_email",
    PKCE_VERIFIER: "pbm_pkce_verifier",
    PKCE_STATE: "pbm_pkce_state",
  };

  let idToken = sessionStorage.getItem(STORAGE.ID_TOKEN) || "";
  let accessToken = sessionStorage.getItem(STORAGE.ACCESS_TOKEN) || "";
  let expiresAt = Number(sessionStorage.getItem(STORAGE.EXPIRES_AT) || 0);

  function isAuthed() {
    return Boolean(idToken) && Date.now() < expiresAt - 15000;
  }

  function setAuth(tokens) {
    idToken = tokens.id_token || "";
    accessToken = tokens.access_token || "";
    const ttlSec = Number(tokens.expires_in || 3600);
    expiresAt = Date.now() + ttlSec * 1000;
    sessionStorage.setItem(STORAGE.ID_TOKEN, idToken);
    sessionStorage.setItem(STORAGE.ACCESS_TOKEN, accessToken);
    sessionStorage.setItem(STORAGE.EXPIRES_AT, String(expiresAt));
    const email = extractEmail(idToken);
    if (email) sessionStorage.setItem(STORAGE.USER_EMAIL, email);
  }

  function clearAuth() {
    idToken = "";
    accessToken = "";
    expiresAt = 0;
    for (const k of Object.values(STORAGE)) sessionStorage.removeItem(k);
  }

  function extractEmail(jwt) {
    try {
      const payload = JSON.parse(
        atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")),
      );
      return payload.email || payload["cognito:username"] || "";
    } catch {
      return "";
    }
  }

  // --- PKCE helpers --------------------------------------------------------
  function randomString(len) {
    const bytes = new Uint8Array(len);
    crypto.getRandomValues(bytes);
    let out = "";
    for (const b of bytes) out += b.toString(16).padStart(2, "0");
    return out.slice(0, len);
  }

  function base64UrlEncode(buf) {
    const bytes = new Uint8Array(buf);
    let binary = "";
    for (const b of bytes) binary += String.fromCharCode(b);
    return btoa(binary)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }

  async function sha256(text) {
    const data = new TextEncoder().encode(text);
    return crypto.subtle.digest("SHA-256", data);
  }

  async function startLogin() {
    if (!CFG.cognitoDomain || !CFG.userPoolClientId || !CFG.redirectUri) {
      showLockedError(
        "Cognito is not configured. Redeploy the CDK stack so config.js " +
          "picks up the Cognito values.",
      );
      return;
    }
    const verifier = randomString(64);
    const state = randomString(32);
    sessionStorage.setItem(STORAGE.PKCE_VERIFIER, verifier);
    sessionStorage.setItem(STORAGE.PKCE_STATE, state);
    const challenge = base64UrlEncode(await sha256(verifier));
    const url =
      CFG.cognitoDomain +
      "/oauth2/authorize" +
      "?response_type=code" +
      "&client_id=" +
      encodeURIComponent(CFG.userPoolClientId) +
      "&redirect_uri=" +
      encodeURIComponent(CFG.redirectUri) +
      "&scope=" +
      encodeURIComponent("openid email profile") +
      "&state=" +
      encodeURIComponent(state) +
      "&code_challenge=" +
      encodeURIComponent(challenge) +
      "&code_challenge_method=S256";
    window.location.assign(url);
  }

  function doLogout() {
    clearAuth();
    if (!CFG.cognitoDomain || !CFG.userPoolClientId) {
      showLocked();
      return;
    }
    const url =
      CFG.cognitoDomain +
      "/logout" +
      "?client_id=" +
      encodeURIComponent(CFG.userPoolClientId) +
      "&logout_uri=" +
      encodeURIComponent(CFG.logoutUri || CFG.redirectUri || "");
    window.location.assign(url);
  }

  async function exchangeCodeForTokens(code) {
    const verifier = sessionStorage.getItem(STORAGE.PKCE_VERIFIER) || "";
    const body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CFG.userPoolClientId,
      code,
      redirect_uri: CFG.redirectUri,
      code_verifier: verifier,
    });
    const resp = await fetch(CFG.cognitoDomain + "/oauth2/token", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error("token exchange failed: " + resp.status + " " + text);
    }
    const tokens = await resp.json();
    setAuth(tokens);
    sessionStorage.removeItem(STORAGE.PKCE_VERIFIER);
    sessionStorage.removeItem(STORAGE.PKCE_STATE);
  }

  async function handleRedirectCallback() {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const state = params.get("state");
    const err = params.get("error");
    if (err) {
      showLockedError(
        "Cognito error: " + err + " - " + (params.get("error_description") || ""),
      );
      return false;
    }
    if (!code) return false;
    const expectedState = sessionStorage.getItem(STORAGE.PKCE_STATE);
    if (!expectedState || state !== expectedState) {
      showLockedError("Login state mismatch - please try again.");
      return false;
    }
    try {
      await exchangeCodeForTokens(code);
    } catch (e) {
      showLockedError(String(e.message || e));
      return false;
    }
    // Remove the code from the URL so a reload does not replay it.
    const cleanUrl =
      window.location.origin + window.location.pathname + window.location.hash;
    window.history.replaceState({}, document.title, cleanUrl);
    return true;
  }

  // --- UI helpers ----------------------------------------------------------
  function showLocked() {
    $("locked-screen").hidden = false;
    $("app-main").hidden = true;
    $("login-btn").hidden = true;
    $("logout-btn").hidden = true;
    $("auth-user").hidden = true;
  }

  function showApp() {
    $("locked-screen").hidden = true;
    $("app-main").hidden = false;
    $("login-btn").hidden = true;
    $("logout-btn").hidden = false;
    const email =
      extractEmail(idToken) || sessionStorage.getItem(STORAGE.USER_EMAIL) || "";
    const userEl = $("auth-user");
    if (email) {
      userEl.textContent = email;
      userEl.hidden = false;
    }
  }

  function showLockedError(msg) {
    const el = $("locked-error");
    el.textContent = msg;
    el.hidden = false;
    showLocked();
  }

  // --- Authenticated API client -------------------------------------------
  async function call(path, opts = {}) {
    if (!API) throw new Error("API URL is not configured");
    if (!isAuthed()) {
      showLocked();
      throw new Error("not authenticated");
    }
    const headers = Object.assign(
      { "content-type": "application/json" },
      opts.headers || {},
      { authorization: "Bearer " + idToken },
    );
    const resp = await fetch(API + path, { ...opts, headers });
    if (resp.status === 401 || resp.status === 403) {
      clearAuth();
      showLocked();
      throw new Error("session expired - please log in again");
    }
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
    if (!isAuthed()) {
      showLocked();
      return;
    }
    try {
      await Promise.all([loadStatus(), loadConfig(), loadSignals(), loadOrders()]);
    } catch (e) {
      console.error(e);
    }
  }

  // --- Wire buttons --------------------------------------------------------
  $("login-btn").addEventListener("click", startLogin);
  $("locked-login-btn").addEventListener("click", startLogin);
  $("logout-btn").addEventListener("click", doLogout);

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

  // --- Bootstrap -----------------------------------------------------------
  (async function init() {
    if (!API || !CFG.userPoolClientId || !CFG.cognitoDomain) {
      document.body.insertAdjacentHTML(
        "afterbegin",
        "<p style='background:#f85149;color:#fff;padding:1rem;text-align:center'>" +
          "Dashboard is not configured. Deploy via CDK so config.js is populated." +
          "</p>",
      );
      showLocked();
      return;
    }

    // If Cognito redirected back here with ?code=..., finish the handshake.
    if (window.location.search.includes("code=")) {
      const ok = await handleRedirectCallback();
      if (!ok) return;
    }

    if (isAuthed()) {
      showApp();
      refreshAll().catch((e) => console.error(e));
      setInterval(() => {
        if (isAuthed()) refreshAll();
        else showLocked();
      }, 15000);
    } else {
      showLocked();
    }
  })();
})();
