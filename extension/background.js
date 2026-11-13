// SpotApply Extension — Background Service Worker

// ── One-time storage migration (hirepath_* → spotapply_*) ────────────────────
// Storage survives an extension update, so a user mid-application when the
// update lands would otherwise lose their pack and the copilot would go quiet.
// Copy any legacy keys forward once, then drop them.
const _LEGACY_KEYS = {
  hirepath_fill_pack: "spotapply_fill_pack",
  hirepath_copilot_pack: "spotapply_copilot_pack",
  hirepath_copilot_ts: "spotapply_copilot_ts",
  hirepath_auto_fill: "spotapply_auto_fill",
  hirepath_auth: "spotapply_auth",
  hirepath_dismissed: "spotapply_dismissed",
};

function migrateLegacyStorage() {
  chrome.storage.local.get(Object.keys(_LEGACY_KEYS), (old) => {
    const present = Object.keys(_LEGACY_KEYS).filter((k) => old[k] !== undefined);
    if (!present.length) return;
    const moved = {};
    present.forEach((k) => { moved[_LEGACY_KEYS[k]] = old[k]; });
    chrome.storage.local.set(moved, () => {
      chrome.storage.local.remove(present);
      console.log("[SpotApply BG] Migrated legacy storage keys:", present.join(", "));
    });
  });
}

// The old copilot session was ONE global pack that any recognised ATS tab
// could resume (audit 2026-09-30: job A's email was typed into an unrelated
// job B). Sessions are now bound to a tab (below); drop the global keys so an
// update cannot leave a stale pack behind.
const _RETIRED_KEYS = ["spotapply_copilot_pack", "spotapply_copilot_ts",
                       "spotapply_auto_fill", "spotapply_pending_tab"];
function retireGlobalSession() { chrome.storage.local.remove(_RETIRED_KEYS); }

chrome.runtime.onInstalled.addListener(() => { migrateLegacyStorage(); retireGlobalSession(); });
chrome.runtime.onStartup.addListener(() => { migrateLegacyStorage(); retireGlobalSession(); });

// ── Trusted ATS hosts ────────────────────────────────────────────────────────
// Exact host or a real subdomain — never a substring. /greenhouse\.io/ matched
// "greenhouse.io.unrelated.example", which then received applicant data.
// Keep in lockstep with ATS_SUFFIXES in content.js (a test compares them).
const ATS_SUFFIXES = [
  "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "workday.com",
  "myworkdaysite.com", "smartrecruiters.com", "avature.net", "icims.com", "taleo.net",
  "successfactors.com", "successfactors.eu", "sapsf.com", "brassring.com", "jobvite.com",
  "workable.com", "bamboohr.com", "recruitee.com", "teamtailor.com", "personio.de",
  "personio.com", "pinpointhq.com", "breezy.hr", "join.com", "rippling.com", "dover.com",
  "paylocity.com", "ultipro.com",
];
function isTrustedATSHost(host) {
  const h = String(host || "").toLowerCase().replace(/\.$/, "");
  return ATS_SUFFIXES.some((d) => h === d || h.endsWith("." + d));
}

// ── Application sessions, one per TAB ────────────────────────────────────────
// A session is created only by an explicit user action — "Auto-Fill & Apply"
// on the dashboard (the tab we open) or "Fill" in the popup (the active tab) —
// and lives in THAT tab: its redirects and the tabs it opens itself (an Apply
// button with target=_blank). Any other tab gets nothing, whatever its host.
const SESSION_MS = 30 * 60 * 1000;   // idle expiry; refreshed while the tab is used
const LAUNCH_MS = 10 * 60 * 1000;    // a launched tab fills off-list hosts this long

let _sessLock = Promise.resolve();
function withSessions(fn) {
  // Serialise read-modify-write: two tabs loading at once must not drop a write.
  const run = _sessLock.then(async () => {
    const s = await chrome.storage.local.get(["spotapply_sessions"]);
    const m = s.spotapply_sessions || {};
    const now = Date.now();
    for (const k of Object.keys(m)) if (!m[k] || now - (m[k].ts || 0) > SESSION_MS) delete m[k];
    const out = await fn(m, now);
    await chrome.storage.local.set({ spotapply_sessions: m });
    return out;
  });
  _sessLock = run.catch(() => {});
  return run;
}

function bindTab(tabId, pack, launched) {
  if (tabId == null || !pack) return Promise.resolve();
  return withSessions((m, now) => {
    const prev = m[tabId];
    m[tabId] = {
      pack, ts: now,
      launchedTs: launched ? now : (prev && prev.pack && prev.pack.app_id === pack.app_id
                                    ? prev.launchedTs || 0 : 0),
    };
  });
}

function sessionFor(tabId, touch) {
  return withSessions((m, now) => {
    const sess = m[tabId];
    if (!sess) return null;
    if (touch) sess.ts = now;
    return sess;
  });
}

function endTabSession(tabId) {
  return withSessions((m) => { delete m[tabId]; });
}

function clearAllSessions() {
  // spotapply_queue too: a run belongs to the account that approved it.
  return chrome.storage.local.remove(["spotapply_sessions", "spotapply_fill_pack", "spotapply_queue",
                                      ..._RETIRED_KEYS]);
}

chrome.tabs.onRemoved.addListener((tabId) => { endTabSession(tabId); });
// An Apply button that opens the form in a NEW tab carries the session with it.
// Chrome's openerTabId alone cannot tell that apart from the user pressing
// Ctrl+T on the application tab: newer Chrome reports the active tab as the
// opener of a blank new tab too (Chrome 153, 2026-10-01), and a session there
// would fill an unrelated site's email box with their details. So a new tab
// inherits only when (1) the user clicked a link or button on the bound page
// in the last few seconds (content.js sends PAGE_CLICK) and (2) its first
// address is a web page, not a blank or New Tab page.
const CLICK_OPEN_MS = 5000;
const _lastPageClick = new Map();     // tabId -> ts of a click on a bound page
const _awaitingFirstUrl = new Map();  // new tabId -> opener tabId
function isWebUrl(u) { return /^https?:/i.test(String(u || "")); }
function inheritSession(tabId, openerId) {
  sessionFor(openerId, false).then((sess) => {
    if (sess && sess.pack) bindTab(tabId, sess.pack, !!sess.launchedTs &&
                                   Date.now() - sess.launchedTs < LAUNCH_MS);
  });
}
chrome.tabs.onCreated.addListener((tab) => {
  if (!tab || tab.openerTabId == null) return;
  const clicked = _lastPageClick.get(tab.openerTabId) || 0;
  if (Date.now() - clicked > CLICK_OPEN_MS) return;
  const first = tab.pendingUrl || tab.url || "";
  if (first) { if (isWebUrl(first)) inheritSession(tab.id, tab.openerTabId); return; }
  _awaitingFirstUrl.set(tab.id, tab.openerTabId);       // decided on its first address
  setTimeout(() => _awaitingFirstUrl.delete(tab.id), 30000);
});
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!changeInfo.url || !_awaitingFirstUrl.has(tabId)) return;
  const opener = _awaitingFirstUrl.get(tabId);
  _awaitingFirstUrl.delete(tabId);
  if (isWebUrl(changeInfo.url)) inheritSession(tabId, opener);
});

// The Supabase user a token belongs to (the JWT "sub"), or "" if unreadable.
function tokenSubject(token) {
  try {
    const part = String(token || "").split(".")[1];
    if (!part) return "";
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json).sub || "";
  } catch (_) { return ""; }
}

// ── Session auth (token refresh) ─────────────────────────────────────────────
// The fill pack carries a short-lived Supabase access token plus (from the
// dashboard) a refresh token and Supabase credentials. We pull those secrets
// OUT of the pack and keep them only in the service worker's private storage —
// they are NEVER forwarded to the content script running on a third-party ATS
// page. The worker uses them to silently refresh the access token whenever an
// authed API call returns 401, so autofill keeps working through long,
// multi-step forms on any site instead of dying when the token expires.

function stashAuth(pack) {
  if (!pack || typeof pack !== "object") return;
  const auth = {};
  if (pack.refresh_token) auth.refresh_token = pack.refresh_token;
  if (pack.supabase_url) auth.supabase_url = pack.supabase_url;
  if (pack.supabase_anon_key) auth.supabase_anon_key = pack.supabase_anon_key;
  if (pack.auth_token) auth.access_token = pack.auth_token;
  // Strip the long-lived secrets so page content scripts can never read them.
  delete pack.refresh_token;
  delete pack.supabase_url;
  delete pack.supabase_anon_key;
  console.log(
    "[SpotApply BG] stashAuth — refresh_token:", !!auth.refresh_token,
    "supabase_url:", !!auth.supabase_url, "access_token:", !!auth.access_token
  );
  if (!auth.refresh_token) {
    console.warn(
      "[SpotApply BG] No refresh token in pack — the access token can't be renewed " +
      "when it expires. Ensure the SpotApply dashboard is up to date and re-click Fill."
    );
  }
  if (!Object.keys(auth).length) return;
  const sub = tokenSubject(auth.access_token);
  if (sub) auth.sub = sub;
  // Merge over any previously stored creds (e.g. keep a rotated refresh token
  // if this pack didn't carry one) — unless the dashboard is now signed in as
  // SOMEONE ELSE: then every stored pack and credential belongs to the other
  // account and is dropped before the new one is kept.
  chrome.storage.local.get(["spotapply_auth"], (s) => {
    const prev = s.spotapply_auth || {};
    if (sub && prev.sub && prev.sub !== sub) {
      console.log("[SpotApply BG] Account changed — clearing stored applications and credentials");
      clearAllSessions().then(() => chrome.storage.local.set({ spotapply_auth: auth }));
      return;
    }
    chrome.storage.local.set({ spotapply_auth: Object.assign({}, prev, auth) });
  });
}

async function doFetch(url, method, token, body) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // Bounded: a hung request used to stall the whole fill with no message.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method: method || "POST",
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    let data = null;
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    const timedOut = err && err.name === "AbortError";
    return { ok: false, timedOut, error: timedOut ? "SpotApply did not answer in time" : err.message };
  } finally {
    clearTimeout(timer);
  }
}
const FETCH_TIMEOUT_MS = 25000;

async function refreshAccessToken(auth) {
  // Exchange the refresh token for a new access token via Supabase's auth API.
  // Supabase rotates the refresh token on each call, so persist the new one.
  if (!auth.refresh_token || !auth.supabase_url || !auth.supabase_anon_key) return null;
  try {
    const res = await fetch(`${auth.supabase_url}/auth/v1/token?grant_type=refresh_token`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "apikey": auth.supabase_anon_key },
      body: JSON.stringify({ refresh_token: auth.refresh_token }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    if (data && data.refresh_token) auth.refresh_token = data.refresh_token;
    return (data && data.access_token) || null;
  } catch (e) {
    return null;
  }
}

// Single-flight token refresh. Supabase ROTATES the refresh token on every
// grant, so two concurrent 401s calling refresh with the same token means the
// second one gets rejected and the session looks dead even though the first
// refresh succeeded. All concurrent callers share one in-flight refresh.
let _refreshInFlight = null;
async function refreshAccessTokenOnce(auth) {
  if (!_refreshInFlight) {
    _refreshInFlight = (async () => {
      try {
        const token = await refreshAccessToken(auth);
        if (token) {
          auth.access_token = token;
          await chrome.storage.local.set({ spotapply_auth: auth });
        }
        return token;
      } finally {
        _refreshInFlight = null;
      }
    })();
  }
  return _refreshInFlight;
}

async function handleApiFetch(payload) {
  const store = await chrome.storage.local.get(["spotapply_auth"]);
  const auth = store.spotapply_auth || {};
  // Prefer the freshest access token the worker holds over the (possibly stale)
  // one the content script sent from its cached pack.
  const token = auth.access_token || payload.token;
  let result = await doFetch(payload.url, payload.method, token, payload.body);

  // Only attempt a refresh for calls that were actually authenticated.
  if (result.status === 401 && payload.token) {
    if (!auth.refresh_token || !auth.supabase_url || !auth.supabase_anon_key) {
      console.warn("[SpotApply BG] 401 but no refresh creds available — cannot renew token");
      result.refreshAvailable = false;
    } else {
      let newToken = await refreshAccessTokenOnce(auth);
      if (!newToken) {
        // A concurrent caller may have just refreshed and rotated the token —
        // re-read storage before declaring the session dead.
        const again = await chrome.storage.local.get(["spotapply_auth"]);
        newToken = (again.spotapply_auth || {}).access_token;
        if (newToken === token) newToken = null; // nothing actually changed
      }
      if (newToken) {
        console.log("[SpotApply BG] Access token refreshed — retrying request");
        result = await doFetch(payload.url, payload.method, newToken, payload.body);
      } else {
        console.warn("[SpotApply BG] Token refresh failed (refresh token rejected/expired)");
        result.refreshAvailable = true;
        result.refreshFailed = true;
      }
    }
  }
  return result;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {

  // Popup "Fill This Form Now" → the user chose this job for the ACTIVE tab.
  // Binds that tab and asks it to fill; the reply is the content script's
  // real result (aborted / outstanding items), never an unconditional ok.
  if (msg.type === "FILL_JOB") {
    console.log("[SpotApply BG] FILL_JOB received from popup");
    stashAuth(msg.payload);
    chrome.storage.local.set({ spotapply_fill_pack: msg.payload }, () => {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (!tabs[0]) { sendResponse({ ok: false, error: "No active tab" }); return; }
        bindTab(tabs[0].id, msg.payload, false).then(() => {
          chrome.tabs.sendMessage(tabs[0].id, { type: "DO_FILL", fillPack: msg.payload }, (res) => {
            if (chrome.runtime.lastError || !res) {
              sendResponse({ ok: false, error: "The form tab did not answer" });
            } else {
              sendResponse(res);
            }
          });
        });
      });
    });
    return true;
  }

  // Dashboard "Auto-Fill & Apply" → open the job in a new tab bound to it.
  if (msg.type === "OPEN_AND_FILL") {
    const pack = msg.payload;
    console.log("[SpotApply BG] OPEN_AND_FILL received for:", pack?.job_title, pack?.apply_url);
    stashAuth(pack);
    // The last launched job, for the popup's explicit Fill button only. It
    // never resumes on its own — sessions live in spotapply_sessions, per tab.
    chrome.storage.local.set({ spotapply_fill_pack: pack }, () => {
      // Open the job NEXT TO the dashboard tab, and inside the same tab group
      // if there is one. A bare tabs.create() lands the tab at the end of the
      // strip outside the group, where it is easy to lose entirely.
      const opener = sender && sender.tab;
      const createOpts = { url: pack.apply_url };
      if (opener) {
        createOpts.index = opener.index + 1;
        createOpts.openerTabId = opener.id;
        if (opener.windowId != null) createOpts.windowId = opener.windowId;
      }
      chrome.tabs.create(createOpts, (tab) => {
        if (chrome.runtime.lastError || !tab) {
          const m = chrome.runtime.lastError?.message || "tab creation failed";
          console.warn("[SpotApply BG] Could not open apply tab:", m);
          sendResponse({ ok: false, error: m });
          return;
        }
        // An explicit click is explicit intent: fill THIS tab wherever its
        // redirects land (board → careers-page.com), for LAUNCH_MS.
        bindTab(tab.id, pack, true);
        console.log("[SpotApply BG] Opened tab", tab.id, "for", pack.apply_url);
        // -1 is TAB_GROUP_ID_NONE; grouping is best-effort.
        if (opener && opener.groupId != null && opener.groupId !== -1 && chrome.tabs.group) {
          try {
            chrome.tabs.group({ groupId: opener.groupId, tabIds: [tab.id] }, () => {
              void chrome.runtime.lastError;   // grouping is a nicety, never fatal
            });
          } catch (e) {
            console.debug("[SpotApply BG] tab grouping unavailable:", e.message);
          }
        }
        sendResponse({ ok: true, tabId: tab.id });
      });
    });
    return true;
  }

  if (msg.type === "INIT_EXTENSION") {
    // The dashboard's init pack is CREDENTIALS ONLY — {url, auth_token,
    // refresh_token, supabase_*} — and stashAuth strips the secrets out of it.
    // It must never become a fill pack: it has no first_name, email or app_id.
    console.log("[SpotApply BG] INIT_EXTENSION received (auth only)");
    stashAuth(msg.payload);
    sendResponse({ ok: true });
    return true;
  }

  // The content script's own tab session: the pack for THIS tab or null.
  // A click on a link or button of a copilot page: a tab it opens in the next
  // few seconds may continue the application (see onCreated).
  if (msg.type === "PAGE_CLICK") {
    const tabId = sender && sender.tab && sender.tab.id;
    if (tabId != null) _lastPageClick.set(tabId, Date.now());
    return false;
  }

  if (msg.type === "GET_TAB_SESSION") {
    const tabId = sender && sender.tab && sender.tab.id;
    if (tabId == null) { sendResponse({ pack: null }); return true; }
    sessionFor(tabId, true).then((sess) => {
      const launched = !!(sess && sess.launchedTs && Date.now() - sess.launchedTs < LAUNCH_MS);
      sendResponse({ pack: sess ? sess.pack : null, launched,
                     attempt: sess ? sess.attempt || null : null });
    });
    return true;
  }

  // The user picked a job for this tab from the page's own Fill button.
  if (msg.type === "BIND_THIS_TAB") {
    const tabId = sender && sender.tab && sender.tab.id;
    bindTab(tabId, msg.pack, false).then(() => sendResponse({ ok: tabId != null }));
    return true;
  }

  // The user pressed Submit. That is an ATTEMPT: the browser may still refuse
  // it (a required field), it may be a login step, or the upload may fail. It
  // is remembered on the tab session — never reported as an application.
  if (msg.type === "SUBMIT_ATTEMPTED") {
    const tabId = sender && sender.tab && sender.tab.id;
    withSessions((m, now) => {
      if (m[tabId]) m[tabId].attempt = { ts: now, url: String(msg.url || "").slice(0, 300) };
    }).then(() => sendResponse({ ok: true }));
    return true;
  }

  // Confirmed: the employer showed a success page, or the user said so. The
  // status is saved FIRST; the session ends and the dashboard refreshes only
  // when the save succeeded, so a failed save can be retried from this tab.
  if (msg.type === "FORM_SUBMITTED") {
    const appId = msg.appId;
    const pack = msg.pack || {};
    const tabId = sender && sender.tab && sender.tab.id;
    console.log("[SpotApply BG] FORM_SUBMITTED (confirmed) for app:", appId, "via", msg.how);
    // spotapply_url is the current key; hirepath_url is the legacy one an
    // older server still sends (installs update independently of deploys).
    const base = pack.spotapply_url || pack.hirepath_url || 'https://app.spotapply.ai';
    handleApiFetch({
      url: `${base}/application/${appId}/submit`,
      method: 'POST',
      token: pack.auth_token,
      body: {},
    }).then((result) => {
      console.log("[SpotApply BG] Submit API result:", result && result.status);
      if (!result || !result.ok) {
        sendResponse({ ok: false, error: (result && result.error) || `HTTP ${result && result.status}` });
        return;
      }
      // An application the apply queue ran: the dashboard hears through the
      // queue's own update (QUEUE_UPDATE) and refreshes its board when the
      // person closes the queue. Reloading the dashboard after each of twenty
      // submissions would throw away whatever they were doing there.
      queueOnSubmitted(tabId, appId).then((ranByQueue) => endTabSession(tabId).then(() => {
        if (!ranByQueue) {
          chrome.tabs.query({}, (tabs) => {
            (tabs || []).forEach((tab) => {
              let h = "";
              try { h = new URL(tab.url || "").hostname; } catch (_) {}
              if (h === "app.spotapply.ai" || h === "localhost" || h === "127.0.0.1") {
                chrome.tabs.sendMessage(tab.id, { type: "DASHBOARD_REFRESH", appId }, () => {
                  void chrome.runtime.lastError;
                });
              }
            });
          });
        }
        sendResponse({ ok: true });
      }));
    });
    return true;
  }

  if (msg.type === "PING") {
    sendResponse({ ok: true, version: chrome.runtime.getManifest().version });
  }

  // ── Apply queue (see the runner below) ─────────────────────────────────────
  // Start / stop / status come from the dashboard page through content.js,
  // which accepts them only from the SpotApply origin and supplies `base`
  // from its own location.
  if (msg.type === "QUEUE_START") {
    queueStart(msg.base, msg.payload || {}).then((queue) => sendResponse({ ok: true, queue }));
    return true;
  }
  if (msg.type === "QUEUE_STOP") {
    queueStop().then((queue) => sendResponse({ ok: true, queue }));
    return true;
  }
  if (msg.type === "QUEUE_STATUS") {
    queueView().then((queue) => sendResponse({ ok: true, queue }));
    return true;
  }
  // A tab the queue opened says how its application went (content.js queueSignal).
  if (msg.type === "QUEUE_SIGNAL") {
    queueOnSignal(sender && sender.tab && sender.tab.id, msg);
    return false;
  }

  // Content script asks the background worker to make a cross-origin API call.
  // Content-script fetches run in the page origin and get blocked by CORS;
  // the service worker has host_permissions and is exempt.
  if (msg.type === "API_FETCH") {
    handleApiFetch(msg.payload || {}).then(sendResponse);
    return true; // keep the message channel open for the async response
  }
});

// NOTE: linkedin.com and indeed.com are deliberately hands-off — their native
// apply flows pre-fill from the user's own account, and automating their pages
// violates their terms (the USER'S account carries the ban risk). SpotApply
// opens those jobs and tracks them hands-off; when a posting redirects to a
// company ATS (greenhouse/workday/…) the copilot fills there as normal.
function isHandsOffHost(h) {
  h = String(h || "").toLowerCase();
  return ["linkedin.com", "indeed.com"].some((d) => h === d || h.endsWith("." + d));
}

// When a BOUND tab finishes loading, fill it. Unbound tabs are never touched.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!tab.url || tab.url.startsWith("chrome")) return;
  let tabHost;
  try { tabHost = new URL(tab.url).hostname; } catch (_) { return; }
  if (isHandsOffHost(tabHost)) return;

  sessionFor(tabId, true).then((sess) => {
    if (!sess || !sess.pack) return;
    // Just submitted from this tab: the page loading now is the employer's
    // answer. Re-filling it would bury a confirmation or the site's errors.
    if (sess.attempt && Date.now() - (sess.attempt.ts || 0) < 2 * 60 * 1000) return;
    const pack = sess.pack;
    const launched = !!(sess.launchedTs && Date.now() - sess.launchedTs < LAUNCH_MS);
    let jobHost = "";
    try { jobHost = new URL(pack.apply_url || "").hostname; } catch (_) {}
    // A launched tab fills wherever its redirects land; after that, only the
    // job's own host or a trusted ATS in the same tab.
    const shouldFill = launched || tabHost === jobHost || isTrustedATSHost(tabHost);
    console.log("[SpotApply BG] Bound tab", tabId, "loaded", tabHost, "— fill:", shouldFill);
    if (!shouldFill) return;
    setTimeout(() => {
      chrome.tabs.sendMessage(tabId, { type: "DO_FILL", fillPack: pack, auto: true }, () => {
        if (chrome.runtime.lastError) {
          // The content script was not there yet (mid-redirect); the next
          // "complete" on this tab retries.
          console.warn("[SpotApply BG] Could not send DO_FILL:", chrome.runtime.lastError.message);
        }
      });
    }, 2000);
  });
});

// ── Apply queue runner ───────────────────────────────────────────────────────
// Founder decision 2026-11-13 (docs/AUTO_APPLY_RESEARCH.md §8). The user
// approves a LIST of applications on the dashboard; this worker applies to
// them ONE AT A TIME in the user's own Chrome:
//
//   ask the server for the next approved item  (POST /api/apply-queue/next)
//   → open its form in the queue's window and bind the tab, exactly as
//     "Auto-Fill & Apply" does (the same fill, the same one-click rules)
//   → wait for the tab to say  attempted  (Submit pressed; the employer's
//     confirmation then arrives through FORM_SUBMITTED, as it always has)
//     or  parked  (a CAPTCHA, a sign-in, a question, a board the queue only
//     fills): the tab stays open for the person and the reason is reported
//   → pause, then the next one.
//
// The server owns the list and every limit (pause, timeout, how many parked
// tabs stop the run); this worker holds only where it is. It never decides
// what may be submitted, never retries a parked form, and has no way to
// report an item as submitted — only as waiting for the person.
//
// MV3: the worker is put to sleep after ~30 s idle, so nothing here lives in
// memory. State is in chrome.storage.local and an alarm wakes the worker to
// take the next step.
const QUEUE_ALARM = "spotapply_queue_tick";
const QUEUE_LIMITS = { gap_seconds: 20, timeout_seconds: 240, max_parked_tabs: 4 };
const QUEUE_CLOSE_DELAY_MS = 4000;      // a confirmed tab stays this long, then closes
const QUEUE_ASK_STALE_MS = 45000;       // an unanswered "next?" may be asked again

function queueBlank() {
  return { state: "idle", reason: "", base: "", windowId: null, current: null, parked: [],
           nextAt: 0, asking: 0, failures: 0, limits: Object.assign({}, QUEUE_LIMITS),
           stats: { submitted: 0, parked: 0 }, token: "" };
}

let _queueLock = Promise.resolve();
/** Read-modify-write of the runner's state, serialised. `fn` must be quick:
 * network calls and tab creation happen OUTSIDE it, so a slow server can never
 * hold up a signal from the tab being filled. */
function withQueue(fn) {
  const run = _queueLock.then(async () => {
    const s = await chrome.storage.local.get(["spotapply_queue"]);
    const q = Object.assign(queueBlank(), s.spotapply_queue || {});
    const out = await fn(q);
    await chrome.storage.local.set({ spotapply_queue: q });
    return out;
  });
  _queueLock = run.catch(() => {});
  return run;
}

/** What the dashboard may see: where the run is, never a token. */
function queuePublic(q) {
  return {
    state: q.state, reason: q.reason || "",
    current: q.current ? { itemId: q.current.itemId, appId: q.current.appId,
                           company: q.current.company || "", title: q.current.title || "",
                           attempted: !!q.current.attemptedAt } : null,
    parked: (q.parked || []).map((p) => ({ itemId: p.itemId, appId: p.appId, reason: p.reason || "" })),
    stats: q.stats || { submitted: 0, parked: 0 },
    version: chrome.runtime.getManifest().version,
  };
}
function queueView() { return withQueue((q) => queuePublic(q)); }

function isDashboardHost(h) {
  return h === "app.spotapply.ai" || h === "localhost" || h === "127.0.0.1";
}
function queueBroadcast() {
  queueView().then((queue) => {
    chrome.tabs.query({}, (tabs) => {
      (tabs || []).forEach((tab) => {
        let h = "";
        try { h = new URL(tab.url || "").hostname; } catch (_) {}
        if (!isDashboardHost(h)) return;
        chrome.tabs.sendMessage(tab.id, { type: "QUEUE_UPDATE", queue }, () => { void chrome.runtime.lastError; });
      });
    });
  });
}

function queueAlarm(on) {
  try {
    if (!chrome.alarms) return;
    if (on) chrome.alarms.create(QUEUE_ALARM, { periodInMinutes: 0.5 });
    else chrome.alarms.clear(QUEUE_ALARM);
  } catch (_) {}
}
/** Take the next step soon (while the worker is awake) — the alarm is the backstop. */
function queueSoon(ms) { setTimeout(() => { queueAdvance(); }, Math.max(50, ms || 50)); }

function queueApi(q, path, body) {
  return handleApiFetch({ url: `${q.base}${path}`, method: "POST", token: q.token || "queue", body: body || {} });
}

/** Where the run stops needing the worker: the alarm goes, the dashboard hears. */
function queueSettle(q, state, reason) {
  q.state = state;
  q.reason = reason || "";
  q.asking = 0;
}

async function queueStart(base, payload) {
  // Credentials first: the pack is auth-only (INIT_EXTENSION's shape) and the
  // long-lived secrets never leave the worker.
  const token = payload && payload.auth_token ? String(payload.auth_token) : "";
  stashAuth(payload);
  await withQueue(async (q) => {
    const tabs = await chrome.tabs.query({});
    const open = new Set((tabs || []).map((t) => t.id));
    q.parked = (q.parked || []).filter((p) => open.has(p.tabId));   // tabs closed while asleep
    if (q.current && !open.has(q.current.tabId)) q.current = null;
    q.base = String(base || q.base || "").replace(/\/+$/, "");
    if (token) q.token = token;
    q.limits = Object.assign({}, QUEUE_LIMITS, q.limits || {});
    if (q.state !== "running") { q.stats = { submitted: 0, parked: 0 }; q.nextAt = 0; q.failures = 0; }
    queueSettle(q, "running", "");
  });
  queueAlarm(true);
  await queueAdvance();
  return queueView();
}

async function queueStop() {
  const tell = await withQueue((q) => {
    // The application being filled right now finishes on its own rules; no
    // new one is started.
    if (q.state !== "running" && q.state !== "paused") return null;
    queueSettle(q, q.current ? "stopping" : "idle", "stopped");
    return q.current ? null : { base: q.base, token: q.token };
  });
  const view = await queueView();
  if (view.state === "idle") queueAlarm(false);
  if (tell) await queueTellServer(tell, "stopped");
  queueBroadcast();
  return view;
}

/** Open the application in the queue's own window (created on the first job,
 * reused after), so twenty forms never take over the window the user is
 * working in. Falls back to an ordinary tab. */
async function queueOpenTab(windowId, url) {
  if (windowId != null) {
    try {
      await chrome.windows.get(windowId);
      const tab = await chrome.tabs.create({ windowId, url, active: true });
      return { tab, windowId };
    } catch (_) { /* the window is gone — make a new one */ }
  }
  try {
    const win = await chrome.windows.create({ url, focused: true, type: "normal" });
    const tab = win && win.tabs && win.tabs[0];
    if (tab) return { tab, windowId: win.id };
  } catch (_) { /* no window API here — an ordinary tab */ }
  try {
    const tab = await chrome.tabs.create({ url, active: true });
    return { tab, windowId: null };
  } catch (_) {
    return { tab: null, windowId: null };
  }
}

let _queueAdvancing = false;
async function queueAdvance() {
  if (_queueAdvancing) return;
  _queueAdvancing = true;
  try { await queueStep(); } catch (e) { console.warn("[SpotApply BG] queue step failed:", e && e.message); }
  finally { _queueAdvancing = false; }
}

async function queueStep() {
  // 1. Decide, under the lock, what this step is.
  const plan = await withQueue((q) => {
    const now = Date.now();
    if (q.state === "stopping" && !q.current) {
      queueSettle(q, "idle", "stopped");
      return { settled: true, tell: { base: q.base, token: q.token, reason: "stopped" } };
    }
    if (q.state !== "running" && q.state !== "stopping") return null;
    if (q.current) {
      // One job with no outcome for too long is parked, not waited on forever.
      const limit = Math.max(30, (q.limits && q.limits.timeout_seconds) || QUEUE_LIMITS.timeout_seconds) * 1000;
      if (now - (q.current.startedAt || now) < limit) return null;
      return { park: queueParkCurrent(q, q.current.attemptedAt ? "unconfirmed" : "timeout") };
    }
    if (q.state !== "running") return null;
    const maxParked = Math.max(1, (q.limits && q.limits.max_parked_tabs) || QUEUE_LIMITS.max_parked_tabs);
    if ((q.parked || []).length >= maxParked) {
      queueSettle(q, "paused", "parked_limit");
      return { settled: true, tell: { base: q.base, token: q.token, reason: "parked_limit" } };
    }
    if (now < (q.nextAt || 0)) return { later: (q.nextAt || 0) - now };
    if (q.asking && now - q.asking < QUEUE_ASK_STALE_MS) return null;
    q.asking = now;
    return { ask: { base: q.base, token: q.token } };
  });
  if (!plan) return;
  if (plan.settled) {
    queueAlarm(false);
    if (plan.tell) await queueTellServer(plan.tell, plan.tell.reason);
    queueBroadcast();
    return;
  }
  if (plan.later) { queueSoon(plan.later + 100); return; }
  if (plan.park) { await queueReportParked(plan.park); queueBroadcast(); queueSoon(200); return; }

  // 2. Ask the server which application is next (outside the lock).
  // `idle`: this worker is holding no application, so one the server still
  // shows as running was orphaned (a crashed tab, a lost worker) — it is
  // parked at once instead of blocking the queue until it times out.
  const res = await queueApi(plan.ask, "/api/apply-queue/next", { idle: true });
  const data = (res && res.data) || {};
  if (!res || !res.ok) {
    const signedOut = res && (res.status === 401 || res.status === 403);
    await withQueue((q) => {
      q.asking = 0;
      q.failures = (q.failures || 0) + 1;
      if (signedOut) queueSettle(q, "paused", "signed_out");
      else if (q.failures >= 3) queueSettle(q, "paused", "server");
      else q.nextAt = Date.now() + 60000;
    });
    queueBroadcast();
    return;
  }
  const limits = {
    gap_seconds: Number(data.gap_seconds) || QUEUE_LIMITS.gap_seconds,
    timeout_seconds: Number(data.timeout_seconds) || QUEUE_LIMITS.timeout_seconds,
    max_parked_tabs: Number(data.max_parked_tabs) || QUEUE_LIMITS.max_parked_tabs,
  };
  if (data.done) {
    // Nothing left (or the day's limit, or switched off): the run is over.
    await withQueue((q) => { q.limits = limits; q.failures = 0; queueSettle(q, "idle", data.reason || "empty"); });
    queueAlarm(false);
    queueBroadcast();
    return;
  }
  if (data.wait || !data.pack || !data.item) {
    const retry = Math.max(1, Number(data.retry_seconds) || 20) * 1000;
    await withQueue((q) => { q.limits = limits; q.asking = 0; q.failures = 0; q.nextAt = Date.now() + retry; });
    queueSoon(retry + 100);
    return;
  }

  // 3. Open it and bind the tab — from here it is an ordinary SpotApply fill.
  const pack = data.pack;
  stashAuth(pack);
  const win = await withQueue((q) => q.windowId);
  const opened = await queueOpenTab(win, pack.apply_url);
  if (!opened.tab) {
    await withQueue((q) => { q.limits = limits; q.asking = 0; q.nextAt = Date.now() + limits.gap_seconds * 1000; });
    await queueReportParked({ base: plan.ask.base, token: plan.ask.token, itemId: data.item.id, reason: "error" });
    queueBroadcast();
    return;
  }
  await bindTab(opened.tab.id, pack, true);
  await chrome.storage.local.set({ spotapply_fill_pack: pack });
  await withQueue((q) => {
    q.limits = limits;
    q.asking = 0;
    q.failures = 0;
    q.windowId = opened.windowId != null ? opened.windowId : q.windowId;
    q.current = { itemId: data.item.id, appId: data.item.application_id, tabId: opened.tab.id,
                  startedAt: Date.now(), attemptedAt: 0, mode: data.item.mode || "",
                  company: data.item.company || "", title: data.item.title || "" };
  });
  console.log("[SpotApply BG] Queue: opened tab", opened.tab.id, "for", pack.apply_url);
  queueBroadcast();
}

/** Move the running item to the parked list. Call INSIDE withQueue; returns
 * what queueReportParked needs once the lock is released. */
function queueParkCurrent(q, reason) {
  const cur = q.current;
  if (!cur) return null;
  q.current = null;
  q.parked = (q.parked || []).concat([{ itemId: cur.itemId, appId: cur.appId, tabId: cur.tabId, reason }]);
  q.stats = q.stats || { submitted: 0, parked: 0 };
  q.stats.parked += 1;
  q.nextAt = Date.now() + Math.max(5, (q.limits && q.limits.gap_seconds) || QUEUE_LIMITS.gap_seconds) * 1000;
  return { base: q.base, token: q.token, itemId: cur.itemId, reason };
}

/** Tell the server an item is waiting for the person. The ONLY outcome this
 * worker can report — "submitted" is the server's own, from the confirmation. */
function queueReportParked(park) {
  if (!park || !park.itemId) return Promise.resolve();
  return queueApi(park, `/api/apply-queue/items/${park.itemId}/outcome`, { reason: park.reason || "needs_you" })
    .catch(() => {});
}

/** The run stopped short of the end (waiting on the person, or stopped): the
 * server raises ONE notification from its own rows. Nothing about any item is
 * claimed here. A finished run needs no call — /next already knows. */
function queueTellServer(q, reason) {
  if (!q || !q.base) return Promise.resolve();
  return queueApi(q, "/api/apply-queue/summary", { reason: reason || "stopped" }).catch(() => {});
}

function queueOnSignal(tabId, msg) {
  if (tabId == null || !msg) return;
  withQueue((q) => {
    const cur = q.current;
    if (!cur || cur.tabId !== tabId || (msg.itemId && cur.itemId !== msg.itemId)) return null;
    if (msg.kind === "attempted") { cur.attemptedAt = Date.now(); return { attempted: true }; }
    if (msg.kind === "parked") return { park: queueParkCurrent(q, String(msg.reason || "needs_you").slice(0, 40)) };
    return null;
  }).then(async (out) => {
    if (!out) return;
    if (out.park) await queueReportParked(out.park);
    queueBroadcast();
    if (out.park) queueSoon(200);
  });
}

/** The application's status was saved as Submitted (FORM_SUBMITTED succeeded).
 * Resolves true when it was the application the queue was running. */
function queueOnSubmitted(tabId, appId) {
  return withQueue((q) => {
    const now = Date.now();
    const cur = q.current;
    if (cur && (cur.tabId === tabId || cur.appId === appId)) {
      q.current = null;
      q.stats = q.stats || { submitted: 0, parked: 0 };
      q.stats.submitted += 1;
      q.nextAt = now + Math.max(5, (q.limits && q.limits.gap_seconds) || QUEUE_LIMITS.gap_seconds) * 1000;
      return { closeTab: cur.tabId, touched: true };
    }
    // A parked one the person finished (or a slow confirmation that arrived
    // after the queue had moved on): it leaves the parked list. Its tab is the
    // person's — never closed for them.
    const before = (q.parked || []).length;
    q.parked = (q.parked || []).filter((p) => p.tabId !== tabId && p.appId !== appId);
    if (q.parked.length === before) return null;
    q.stats = q.stats || { submitted: 0, parked: 0 };
    q.stats.submitted += 1;
    q.stats.parked = Math.max(0, (q.stats.parked || 0) - 1);
    return { touched: true, resume: queueResumeIfRoom(q) };
  }).then((out) => {
    if (!out) return false;
    if (out.closeTab != null) {
      // The confirmation page has been read and saved; the tab is done.
      setTimeout(() => { chrome.tabs.remove(out.closeTab, () => { void chrome.runtime.lastError; }); },
                 QUEUE_CLOSE_DELAY_MS);
    }
    if (out.resume) queueAlarm(true);
    queueBroadcast();
    queueSoon(200);
    return out.closeTab != null;
  }).catch(() => false);
}

/** Paused only because too many tabs were waiting, and one just cleared. */
function queueResumeIfRoom(q) {
  const maxParked = Math.max(1, (q.limits && q.limits.max_parked_tabs) || QUEUE_LIMITS.max_parked_tabs);
  if (q.state === "paused" && q.reason === "parked_limit" && (q.parked || []).length < maxParked) {
    queueSettle(q, "running", "");
    return true;
  }
  return false;
}

chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  withQueue((q) => {
    const cur = q.current;
    // Closing the queue's own window is the person saying stop.
    const windowClosing = !!(removeInfo && removeInfo.isWindowClosing && q.windowId != null &&
                             removeInfo.windowId === q.windowId);
    let out = null;
    if (cur && cur.tabId === tabId) {
      const park = queueParkCurrent(q, cur.attemptedAt ? "unconfirmed" : "tab_closed");
      q.parked = (q.parked || []).filter((p) => p.tabId !== tabId);   // nothing is open to come back to
      out = { park };
    } else if ((q.parked || []).some((p) => p.tabId === tabId)) {
      q.parked = q.parked.filter((p) => p.tabId !== tabId);
      out = { resume: queueResumeIfRoom(q) };
    }
    if (windowClosing) {
      q.windowId = null;
      if (q.state === "running" || q.state === "stopping" || q.state === "paused") {
        queueSettle(q, "idle", "window_closed");
        out = Object.assign(out || {}, { stopped: true, tell: { base: q.base, token: q.token } });
      }
    }
    return out;
  }).then(async (out) => {
    if (!out) return;
    if (out.park) await queueReportParked(out.park);
    if (out.tell) await queueTellServer(out.tell, "window_closed");
    if (out.stopped) queueAlarm(false);
    else if (out.resume) queueAlarm(true);
    queueBroadcast();
    if (!out.stopped) queueSoon(200);
  });
});

if (chrome.alarms && chrome.alarms.onAlarm) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === QUEUE_ALARM) queueAdvance();
  });
}
// An extension update restarts the worker mid-run and drops its alarm: the
// run is still in storage, so wake it again.
chrome.runtime.onInstalled.addListener(() => {
  withQueue((q) => q.state).then((state) => {
    if (state === "running" || state === "stopping") { queueAlarm(true); queueSoon(1000); }
  });
});
// A browser restart wakes the worker with a run still marked in storage: its
// tabs are gone, so the run is over (the server parks the orphaned item).
chrome.runtime.onStartup.addListener(() => {
  withQueue((q) => {
    q.current = null; q.parked = []; q.windowId = null;
    if (q.state !== "idle") queueSettle(q, "idle", "browser_restarted");
  }).then(() => queueAlarm(false));
});
