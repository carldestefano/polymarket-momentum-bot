(function () {
  "use strict";

  const CFG = window.SCANNER_CONFIG || {};
  const API = (CFG.apiUrl || window.SCANNER_API_URL || "").replace(/\/+$/, "");
  const $ = (id) => document.getElementById(id);

  function apiUrl(path) {
    const p = path.startsWith("/") ? path : "/" + path;
    return API + p;
  }

  function describeFetchError(err, url) {
    // A TypeError with no status is almost always a CORS preflight
    // failure, DNS/connection error, or mixed-content block. Surface the
    // URL so the operator can see which host is unreachable.
    const msg = err && err.message ? err.message : String(err);
    if (err instanceof TypeError) {
      return (
        "Network/CORS error reaching " +
        (url || "API") +
        " (" +
        msg +
        "). Check that the API CORS config allows this origin and " +
        "that the API URL in config.js is correct."
      );
    }
    return msg;
  }

  const STORAGE = {
    ID_TOKEN: "pbs_id_token",
    ACCESS_TOKEN: "pbs_access_token",
    EXPIRES_AT: "pbs_expires_at",
    USER_EMAIL: "pbs_user_email",
    PKCE_VERIFIER: "pbs_pkce_verifier",
    PKCE_STATE: "pbs_pkce_state",
  };

  let idToken = sessionStorage.getItem(STORAGE.ID_TOKEN) || "";
  let accessToken = sessionStorage.getItem(STORAGE.ACCESS_TOKEN) || "";
  let expiresAt = Number(sessionStorage.getItem(STORAGE.EXPIRES_AT) || 0);
  let lastOpps = [];
  let lastMeta = null;

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

  // --- PKCE ---------------------------------------------------------------
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
      "/login?" +
      new URLSearchParams({
        client_id: CFG.userPoolClientId,
        response_type: "code",
        scope: "openid email profile",
        redirect_uri: CFG.redirectUri,
        code_challenge: challenge,
        code_challenge_method: "S256",
        state,
      }).toString();
    window.location.assign(url);
  }

  async function exchangeCode(code) {
    const verifier = sessionStorage.getItem(STORAGE.PKCE_VERIFIER);
    if (!verifier) throw new Error("missing PKCE verifier");
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
    if (!resp.ok) throw new Error("token exchange failed: " + resp.status);
    return resp.json();
  }

  function logout() {
    clearAuth();
    if (CFG.cognitoDomain && CFG.userPoolClientId && CFG.logoutUri) {
      const url =
        CFG.cognitoDomain +
        "/logout?" +
        new URLSearchParams({
          client_id: CFG.userPoolClientId,
          logout_uri: CFG.logoutUri,
        }).toString();
      window.location.assign(url);
    } else {
      renderAuth();
    }
  }

  // --- API ----------------------------------------------------------------
  async function apiGet(path) {
    const url = apiUrl(path);
    let resp;
    try {
      resp = await fetch(url, {
        headers: { authorization: "Bearer " + idToken },
      });
    } catch (e) {
      console.error("apiGet fetch failed", { url, error: e });
      throw new Error(describeFetchError(e, url));
    }
    if (resp.status === 401) {
      clearAuth();
      renderAuth();
      throw new Error("unauthorized");
    }
    if (resp.status === 403) {
      throw new Error("API " + path + " -> 403 forbidden (token rejected)");
    }
    if (!resp.ok) throw new Error("API " + path + " -> " + resp.status);
    return resp.json();
  }

  async function apiPost(path, body) {
    const url = apiUrl(path);
    let resp;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers: {
          authorization: "Bearer " + idToken,
          "content-type": "application/json",
        },
        body: JSON.stringify(body || {}),
      });
    } catch (e) {
      console.error("apiPost fetch failed", { url, error: e });
      throw new Error(describeFetchError(e, url));
    }
    if (resp.status === 401) {
      clearAuth();
      renderAuth();
      throw new Error("unauthorized");
    }
    if (resp.status === 403) {
      throw new Error("API " + path + " -> 403 forbidden (token rejected)");
    }
    if (!resp.ok) throw new Error("API " + path + " -> " + resp.status);
    return resp.json();
  }

  // --- Rendering ----------------------------------------------------------
  function showLockedError(msg) {
    const el = $("locked-error");
    el.textContent = msg;
    el.hidden = false;
  }

  function setApiError(msg) {
    const el = $("api-error");
    if (!msg) {
      el.hidden = true;
      el.textContent = "";
    } else {
      el.hidden = false;
      el.textContent = msg;
    }
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined || isNaN(Number(v))) return "–";
    return Number(v).toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function fmtCents(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "–";
    return Number(v).toFixed(3);
  }

  function fmtMoney(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return "–";
    const n = Number(v);
    if (n >= 1_000_000) return "$" + (n / 1_000_000).toFixed(2) + "M";
    if (n >= 1_000) return "$" + (n / 1_000).toFixed(1) + "k";
    return "$" + n.toFixed(0);
  }

  function fmtDuration(sec) {
    if (sec === null || sec === undefined || isNaN(Number(sec))) return "–";
    const s = Number(sec);
    if (s < 0) return "closed";
    if (s < 3600) return Math.round(s / 60) + "m";
    if (s < 86400) return (s / 3600).toFixed(1) + "h";
    return (s / 86400).toFixed(1) + "d";
  }

  function renderOpps(filtered) {
    const tbody = $("opps-table").querySelector("tbody");
    tbody.innerHTML = "";
    if (!filtered.length) {
      $("opps-empty").hidden = false;
      return;
    }
    $("opps-empty").hidden = true;
    filtered.forEach((o, i) => {
      const tr = document.createElement("tr");
      const link = o.url
        ? `<a href="${o.url}" target="_blank" rel="noopener">${escapeHtml(o.question || o.slug || "(market)")}</a>`
        : escapeHtml(o.question || "(market)");
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td>${link}</td>
        <td>${o.threshold_usd ? "$" + fmtNum(o.threshold_usd, 0) : "–"}</td>
        <td>${fmtCents(o.best_bid)}</td>
        <td>${fmtCents(o.best_ask)}</td>
        <td>${fmtCents(o.mid)}</td>
        <td>${fmtCents(o.spread)}</td>
        <td>${fmtCents(o.last_price)}</td>
        <td>${o.fair_value !== null && o.fair_value !== undefined ? Number(o.fair_value).toFixed(3) : "–"}</td>
        <td>${o.edge !== null && o.edge !== undefined ? Number(o.edge).toFixed(3) : "–"}</td>
        <td>${fmtMoney(o.volume_usd)}</td>
        <td>${fmtMoney(o.liquidity_usd)}</td>
        <td>${fmtDuration(o.seconds_to_resolution)}</td>
        <td>${fmtNum(o.score, 2)}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  function applyFilters() {
    const minLiq = Number($("filter-liq").value || 0);
    const maxDaysRaw = $("filter-days").value;
    const maxDays = maxDaysRaw === "" ? null : Number(maxDaysRaw);
    const shortOnly = $("filter-short").checked;
    const filtered = lastOpps.filter((o) => {
      if (minLiq > 0 && (!o.liquidity_usd || o.liquidity_usd < minLiq)) return false;
      if (maxDays !== null && (!o.seconds_to_resolution || o.seconds_to_resolution / 86400 > maxDays)) return false;
      if (shortOnly) {
        if (!o.seconds_to_resolution || o.seconds_to_resolution > 7 * 86400) return false;
      }
      return true;
    });
    renderOpps(filtered);
  }

  function renderScans(scans) {
    const tbody = $("scans-table").querySelector("tbody");
    tbody.innerHTML = "";
    scans.forEach((s) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(s.scanned_at || "")}</td>
        <td>${s.btc_price_usd ? "$" + fmtNum(s.btc_price_usd, 0) : "–"}</td>
        <td>${s.total_markets ?? "–"}</td>
        <td>${s.btc_markets ?? "–"}</td>
        <td>${s.top_n ?? "–"}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function renderChips(meta) {
    $("btc-price-chip").textContent =
      "BTC: " + (meta && meta.btc_price_usd ? "$" + fmtNum(meta.btc_price_usd, 0) : "?");
    $("scan-time-chip").textContent =
      "last scan: " + (meta && meta.scanned_at ? meta.scanned_at : "?");
    $("btc-count-chip").textContent =
      "BTC markets: " + (meta && meta.btc_markets != null ? meta.btc_markets : "?");
  }

  async function refresh() {
    setApiError("");
    if (!API) {
      setApiError(
        "API URL is not configured. Redeploy the CDK stack so " +
          "config.js picks up the ApiUrl output.",
      );
      return;
    }
    if (!/^https:\/\//i.test(API)) {
      setApiError(
        "API URL must be absolute HTTPS, got: " + API + ". " +
          "Check config.js.",
      );
      return;
    }
    try {
      const [status, opps, scans] = await Promise.all([
        apiGet("/status"),
        apiGet("/opportunities?limit=50"),
        apiGet("/scans?limit=20"),
      ]);
      lastMeta = (opps && (opps.items ? opps : null))
        ? { btc_price_usd: opps.btc_price_usd, scanned_at: opps.scanned_at, btc_markets: (status && status.latest ? status.latest.btc_markets : null) }
        : null;
      renderChips(lastMeta);
      lastOpps = (opps && opps.items) || [];
      applyFilters();
      renderScans((scans && scans.items) || []);
    } catch (e) {
      if (String(e.message || e) !== "unauthorized") {
        setApiError("API error: " + (e.message || e));
      }
    }
  }

  async function triggerScan() {
    setApiError("");
    try {
      await apiPost("/scan", {});
      // Give the Lambda a few seconds, then refresh.
      setTimeout(refresh, 5000);
    } catch (e) {
      setApiError("Trigger failed: " + (e.message || e));
    }
  }

  // --- Boot ---------------------------------------------------------------
  function renderAuth() {
    if (isAuthed()) {
      $("locked-screen").hidden = true;
      $("app-main").hidden = false;
      $("logout-btn").hidden = false;
      const email = sessionStorage.getItem(STORAGE.USER_EMAIL) || "";
      const chip = $("auth-user");
      if (email) {
        chip.textContent = email;
        chip.hidden = false;
      } else {
        chip.hidden = true;
      }
    } else {
      $("locked-screen").hidden = false;
      $("app-main").hidden = true;
      $("logout-btn").hidden = true;
      $("auth-user").hidden = true;
    }
  }

  async function handleOAuthRedirect() {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const state = params.get("state");
    if (!code) return false;
    const expected = sessionStorage.getItem(STORAGE.PKCE_STATE);
    if (!expected || expected !== state) {
      showLockedError("OAuth state mismatch; please log in again.");
      return false;
    }
    try {
      const tokens = await exchangeCode(code);
      setAuth(tokens);
    } catch (e) {
      showLockedError("Token exchange failed: " + (e.message || e));
      return false;
    }
    // Clean URL.
    const clean = window.location.origin + window.location.pathname;
    window.history.replaceState({}, document.title, clean);
    return true;
  }

  async function boot() {
    $("locked-login-btn").addEventListener("click", startLogin);
    $("logout-btn").addEventListener("click", logout);
    $("refresh").addEventListener("click", refresh);
    $("trigger-scan").addEventListener("click", triggerScan);
    ["filter-liq", "filter-days", "filter-short"].forEach((id) =>
      $(id).addEventListener("input", applyFilters),
    );

    await handleOAuthRedirect();
    renderAuth();
    if (isAuthed()) {
      refresh();
      // Auto-refresh every 60 seconds while visible.
      setInterval(() => {
        if (document.visibilityState === "visible" && isAuthed()) refresh();
      }, 60000);
    }
  }

  boot();
})();
