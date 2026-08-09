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

chrome.runtime.onInstalled.addListener(migrateLegacyStorage);
chrome.runtime.onStartup.addListener(migrateLegacyStorage);

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
  // Merge over any previously stored creds (e.g. keep a rotated refresh token
  // if this pack didn't carry one).
  chrome.storage.local.get(["spotapply_auth"], (s) => {
    chrome.storage.local.set({ spotapply_auth: Object.assign({}, s.spotapply_auth || {}, auth) });
  });
}

async function doFetch(url, method, token, body) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  try {
    const res = await fetch(url, {
      method: method || "POST",
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

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

  // Popup sends FILL_JOB → send DO_FILL to the currently active tab
  if (msg.type === "FILL_JOB") {
    console.log("[SpotApply BG] FILL_JOB received from popup");
    stashAuth(msg.payload);
    chrome.storage.local.set({ spotapply_fill_pack: msg.payload, spotapply_auto_fill: false }, () => {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (!tabs[0]) { sendResponse({ ok: false, error: "No active tab" }); return; }
        console.log("[SpotApply BG] Sending DO_FILL to tab", tabs[0].id, tabs[0].url);
        chrome.tabs.sendMessage(tabs[0].id, { type: "DO_FILL", fillPack: msg.payload }, (res) => {
          sendResponse(res || { ok: true });
        });
      });
    });
    return true;
  }

  // Content script (on dashboard page) sends OPEN_AND_FILL → open job tab, then fill when loaded
  if (msg.type === "OPEN_AND_FILL") {
    const pack = msg.payload;
    console.log("[SpotApply BG] OPEN_AND_FILL received for:", pack?.job_title, pack?.apply_url);
    stashAuth(pack);
    // Store BOTH a one-shot auto_fill flag AND a persistent copilot session.
    // The copilot session (30-min window) lets autofill survive cross-domain
    // navigations — e.g. accenture.com → myworkdayjobs.com after clicking Apply.
    chrome.storage.local.set({
      spotapply_fill_pack: pack,
      spotapply_auto_fill: true,
      spotapply_copilot_pack: pack,
      spotapply_copilot_ts: Date.now(),
    }, () => {
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
      // Remember WHICH tab the user launched. The one-shot auto_fill flag is
      // consumed by the first "complete" event, so a job board that redirects
      // (board → careers-page.com) burned it on the intermediate page and the
      // real form was never filled — with no error, since the destination host
      // is not on the ATS allow-list either. An explicit click is explicit
      // intent: fill THIS tab whatever it lands on.
      chrome.tabs.create(createOpts, (tab) => {
        if (tab && tab.id != null) {
          chrome.storage.local.set({
            spotapply_pending_tab: { tabId: tab.id, ts: Date.now() },
          });
        }
        if (chrome.runtime.lastError || !tab) {
          const msg = chrome.runtime.lastError?.message || "tab creation failed";
          console.warn("[SpotApply BG] Could not open apply tab:", msg);
          sendResponse({ ok: false, error: msg });
          return;
        }
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
    // It must never become the copilot's fill pack: it has no first_name,
    // email or app_id, so filling from it wrote "undefined undefined" into
    // every name field and nothing anywhere else. Worse, the dashboard
    // re-broadcasts this every 15s, so it used to overwrite the REAL pack of
    // an application already in progress. Stash the auth, touch nothing else.
    console.log("[SpotApply BG] INIT_EXTENSION received (auth only)");
    stashAuth(msg.payload);
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "FORM_SUBMITTED") {
    const appId = msg.appId;
    const pack = msg.pack;
    console.log("[SpotApply BG] FORM_SUBMITTED received for app:", appId);
    
    // 1. Send submit API call to the backend
    // spotapply_url is the current key; hirepath_url is the legacy one an
    // older server still sends (installs update independently of deploys).
    const base = pack.spotapply_url || pack.hirepath_url || 'https://app.spotapply.ai';
    const url = `${base}/application/${appId}/submit`;
    handleApiFetch({
      url: url,
      method: 'POST',
      token: pack.auth_token,
      body: {}
    }).then(result => {
      console.log("[SpotApply BG] Submit API result:", result);
    });

    // 2. Broadcast DASHBOARD_REFRESH message to any dashboard tabs
    chrome.tabs.query({}, (tabs) => {
      (tabs || []).forEach(tab => {
        if (tab.url && (tab.url.includes("app.spotapply.ai") || tab.url.includes("localhost") || tab.url.includes("127.0.0.1"))) {
          console.log("[SpotApply BG] Sending DASHBOARD_REFRESH to tab", tab.id);
          chrome.tabs.sendMessage(tab.id, { type: "DASHBOARD_REFRESH", appId: appId }, () => {
            if (chrome.runtime.lastError) {
              // ignore
            }
          });
        }
      });
    });
    
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === "PING") {
    sendResponse({ ok: true, version: chrome.runtime.getManifest().version });
  }

  // Content script asks the background worker to make a cross-origin API call.
  // Content-script fetches run in the page origin and get blocked by CORS;
  // the service worker has host_permissions and is exempt.
  if (msg.type === "API_FETCH") {
    handleApiFetch(msg.payload || {}).then(sendResponse);
    return true; // keep the message channel open for the async response
  }
});

// NOTE: linkedin.com and indeed.com are deliberately NOT here — their native
// apply flows pre-fill from the user's own account, and automating their pages
// violates their terms (the USER'S account carries the ban risk). SpotApply
// opens those jobs and tracks them hands-off; when a posting redirects to a
// company ATS (greenhouse/workday/…) the copilot fills there as normal.
// Keep in lockstep with isKnownATS() in content.js, and with the discovery
// sources in app/discovery/ — every board we FIND jobs on is a board the
// copilot has to recognise. The second row was missing, so on those hosts
// isKnownATS was false: the fill only worked on the tab we opened ourselves
// (exact host match) and the session could not resume across a multi-step
// form or a cross-domain hop.
const ATS_HOSTS = /greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|workday\.com|smartrecruiters\.com|avature\.net|icims\.com|taleo\.net|successfactors|brassring|jobvite\.com|workable\.com|bamboohr\.com|recruitee\.com|teamtailor\.com|personio\.(de|com)|pinpointhq\.com|breezy\.hr|join\.com|rippling\.com|dover\.com|paylocity\.com|ultipro\.com|myworkdaysite\.com/i;

// When a tab finishes loading, check if we should auto-fill it
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!tab.url || tab.url.startsWith("chrome")) return;

  chrome.storage.local.get(
    ["spotapply_fill_pack", "spotapply_auto_fill", "spotapply_copilot_pack",
     "spotapply_copilot_ts", "spotapply_pending_tab"],
    (data) => {
      const pack = data.spotapply_fill_pack || data.spotapply_copilot_pack;
      if (!pack) {
        console.log("[SpotApply BG] Tab", tabId, "loaded but no pack in storage — skipping");
        return;
      }

      let tabHost;
      try { tabHost = new URL(tab.url).hostname; } catch (_) { return; }

      // LinkedIn + Indeed: never DO_FILL, on any page. Their native apply
      // flows pre-fill from the user's own account, and automating their
      // pages violates their terms (the USER'S account takes the ban risk).
      // This also covers the auto_fill same-host path when apply_url points
      // at them. The LinkedIn profile-import card is separate and unaffected.
      if (tabHost.includes("linkedin.com") || tabHost.includes("indeed.com")) {
        console.log("[SpotApply BG] LinkedIn/Indeed page — hands-off (native apply pre-fills)");
        return;
      }

      // Diagnostic dump
      console.log("[SpotApply BG] === Tab loaded:", tabId, tabHost, "===");
      console.log("[SpotApply BG]   auto_fill:", data.spotapply_auto_fill);
      console.log("[SpotApply BG]   copilot_pack:", !!data.spotapply_copilot_pack);
      console.log("[SpotApply BG]   copilot_ts:", data.spotapply_copilot_ts);
      console.log("[SpotApply BG]   ATS match:", ATS_HOSTS.test(tabHost));

      // Determine if this tab should receive DO_FILL:
      // 1. One-shot flag set when we opened the tab (exact host match OR known ATS)
      // 2. Persistent copilot session active (30-min window) on any ATS/actionable page
      const SESSION_MS = 30 * 60 * 1000;
      const sessionAge = data.spotapply_copilot_ts ? (Date.now() - data.spotapply_copilot_ts) : Infinity;
      const freshSession = sessionAge < SESSION_MS;
      // NOTE: a pack sitting on an ATS host is NOT enough — the session must
      // be fresh, or last week's pack would fill an unrelated application.

      // A tab the user explicitly launched from "Auto-Fill & Apply" is filled
      // wherever it ends up, allow-list or not, for a bounded window.
      const pendingTab = data.spotapply_pending_tab;
      const PENDING_MS = 10 * 60 * 1000;
      const userLaunched = !!(pendingTab && pendingTab.tabId === tabId &&
                              (Date.now() - (pendingTab.ts || 0)) < PENDING_MS);

      let shouldFill = userLaunched;
      if (userLaunched) {
        console.log("[SpotApply BG]   user-launched tab — filling regardless of host");
      }
      if (data.spotapply_auto_fill) {
        try {
          const jobHost = new URL(pack.apply_url || "").hostname;
          // Same host OR tab is a known ATS (handles accenture → workday cross-domain)
          if (tabHost === jobHost || ATS_HOSTS.test(tabHost)) shouldFill = true;
        } catch (_) {
          if (ATS_HOSTS.test(tabHost)) shouldFill = true;
        }
      } else if (freshSession && ATS_HOSTS.test(tabHost)) {
        // Copilot session: resume on any ATS page while the session is fresh
        shouldFill = true;
      }

      console.log("[SpotApply BG]   shouldFill:", shouldFill, "freshSession:", freshSession);

      if (!shouldFill) return;

      console.log("[SpotApply BG] ▶ Tab", tabId, "matched — sending DO_FILL in 2s");
      if (data.spotapply_auto_fill) chrome.storage.local.set({ spotapply_auto_fill: false });

      setTimeout(() => {
        chrome.tabs.sendMessage(tabId, { type: "DO_FILL", fillPack: pack }, (res) => {
          if (chrome.runtime.lastError) {
            // Keep the pending marker: the content script was not there yet
            // (mid-redirect), so the next "complete" on this tab retries.
            console.warn("[SpotApply BG] Could not send DO_FILL:", chrome.runtime.lastError.message);
          } else {
            console.log("[SpotApply BG] DO_FILL sent, response:", res);
            if (userLaunched) chrome.storage.local.remove("spotapply_pending_tab");
          }
        });
      }, 2000);
    }
  );
});
