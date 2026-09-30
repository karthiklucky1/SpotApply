// popup.js — SpotApply Extension Popup

const SPOTAPPLY_URL = "https://app.spotapply.ai";

function showStatus(msg, type) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = "status " + type;
  el.style.display = "block";
}

// Load stored job pack
chrome.storage.local.get(["spotapply_fill_pack"], (data) => {
  if (data.spotapply_fill_pack) {
    const pack = data.spotapply_fill_pack;
    document.getElementById("job-section").style.display = "block";
    document.getElementById("job-title").textContent =
      (pack.job_title || "Unknown Role") + (pack.company ? ` @ ${pack.company}` : "");
    try {
      document.getElementById("job-meta").textContent = new URL(pack.apply_url).hostname;
    } catch (e) {
      document.getElementById("job-meta").textContent = pack.apply_url || "";
    }
  } else {
    document.getElementById("no-job-section").style.display = "block";
  }
});

// Fill button
document.getElementById("btn-fill")?.addEventListener("click", () => {
  const btn = document.getElementById("btn-fill");
  btn.disabled = true;
  btn.textContent = "⏳ Filling...";

  chrome.storage.local.get(["spotapply_fill_pack"], (data) => {
    if (!data.spotapply_fill_pack) {
      showStatus("No job loaded. Go to SpotApply and click Fill with Extension.", "err");
      btn.disabled = false;
      btn.textContent = "⚡ Fill This Form Now";
      return;
    }
    chrome.runtime.sendMessage(
      { type: "FILL_JOB", payload: data.spotapply_fill_pack },
      (res) => {
        // The content script's own account of what happened — an aborted fill
        // is never shown as done, and "ready" never means the employer accepted.
        showStatus(fillOutcomeMessage(res, chrome.runtime.lastError), res && res.ok &&
                   res.state === "ready_for_review" ? "ok" : (res && res.ok ? "info" : "err"));
        btn.disabled = false;
        btn.textContent = "⚡ Fill This Form Now";
      }
    );
  });
});

function fillOutcomeMessage(res, lastError) {
  if (lastError || !res) return "Could not reach the form tab. Make sure the job form tab is active and open.";
  if (!res.ok) return "⚠️ Nothing was filled: " + (res.reason || "the fill was stopped.");
  if (res.state === "opening_form") return "Looking for the application form on this page…";
  const parts = [];
  if (typeof res.filled === "number") parts.push(`${res.filled} filled`);
  if (res.needUser) parts.push(`${res.needUser} need you`);
  if (res.failed) parts.push(`${res.failed} with a problem`);
  const head = res.state === "ready_for_review"
    ? "✅ Ready for your review"
    : "⚠️ Needs your attention";
  const probs = (res.problems || []).slice(0, 3).join(" · ");
  return `${head}${parts.length ? " — " + parts.join(", ") : ""}.${probs ? " " + probs : ""} Check every field before you submit.`;
}

// Dashboard links
["btn-dash", "btn-dash2"].forEach((id) => {
  document.getElementById(id)?.addEventListener("click", () => {
    chrome.tabs.create({ url: SPOTAPPLY_URL + "/dashboard" });
  });
});

// ── Free ghost-check / fit-check on the current tab ──────────────────────────
// Works with no account: ghost score + freshness. Signed-in users (auth token
// stashed by the dashboard) also get a keyword fit-check vs their resume.
function showCheckStatus(msg, type) {
  const el = document.getElementById("check-status");
  el.textContent = msg;
  el.className = "status " + type;
  el.style.display = "block";
}

document.getElementById("btn-check")?.addEventListener("click", () => {
  const btn = document.getElementById("btn-check");
  btn.disabled = true;
  btn.textContent = "⏳ Checking…";
  document.getElementById("check-result").style.display = "none";
  document.getElementById("check-status").style.display = "none";

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tabUrl = tabs && tabs[0] && tabs[0].url;
    if (!tabUrl || !/^https?:/.test(tabUrl)) {
      showCheckStatus("Open a job posting tab first.", "err");
      btn.disabled = false;
      btn.textContent = "🔍 Is this job real? Check it";
      return;
    }
    chrome.storage.local.get(["spotapply_fill_pack", "spotapply_auth"], async (data) => {
      const pack = data.spotapply_fill_pack || {};
      const base = pack.spotapply_url || pack.hirepath_url || SPOTAPPLY_URL;
      const token = pack.auth_token || (data.spotapply_auth && data.spotapply_auth.access_token) || null;
      try {
        const res = await fetch(`${base}/api/public/job-check`, {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" },
            token ? { Authorization: `Bearer ${token}` } : {}),
          body: JSON.stringify({ url: tabUrl }),
        });
        const d = await res.json();
        if (!res.ok || d.ok === false) throw new Error(d.error || `HTTP ${res.status}`);

        const verdictEl = document.getElementById("check-verdict");
        const detailEl = document.getElementById("check-detail");
        const fitEl = document.getElementById("check-fit");
        if (d.live === false) {
          verdictEl.textContent = "👻 Closed or removed — don't waste an application";
          verdictEl.style.color = "#f87171";
        } else if (d.ghost_score >= 0.6) {
          verdictEl.textContent = `⚠️ Likely ghost job (score ${Math.round(d.ghost_score * 100)}%)`;
          verdictEl.style.color = "#fbbf24";
        } else if (d.live === null) {
          verdictEl.textContent = "ℹ️ Can't verify this site automatically";
          verdictEl.style.color = "#94a3b8";
        } else {
          verdictEl.textContent = `✅ Looks real (ghost score ${Math.round(d.ghost_score * 100)}%)`;
          verdictEl.style.color = "#34d399";
        }
        const bits = [];
        if (d.title) bits.push(d.title + (d.company ? ` @ ${d.company}` : ""));
        if (typeof d.posted_days_ago === "number") bits.push(`posted ${d.posted_days_ago}d ago`);
        if (d.signals && d.signals.length) bits.push(d.signals.join(" · ").replace(/_/g, " "));
        detailEl.textContent = bits.join(" — ");
        if (d.fit) {
          fitEl.innerHTML = `<b style="color:#818cf8">Your keyword fit: ${d.fit.score_pct}%</b>` +
            (d.fit.missing && d.fit.missing.length
              ? ` — missing: ${d.fit.missing.slice(0, 6).map(s => String(s).replace(/[<>&]/g, "")).join(", ")}` : "");
        } else {
          fitEl.textContent = token ? "" : "Sign in to SpotApply to see your keyword fit for this job.";
        }
        document.getElementById("check-result").style.display = "block";
      } catch (e) {
        showCheckStatus("Check failed: " + e.message, "err");
      }
      btn.disabled = false;
      btn.textContent = "🔍 Is this job real? Check it";
    });
  });
});

// ── EEO opt-in ───────────────────────────────────────────────────────────────
// Voluntary self-identification is never answered for the user unless they say
// so here; the content script reads this key and reacts to changes live.
const eeoBox = document.getElementById("eeo-optin");
if (eeoBox) {
  chrome.storage.local.get(["spotapply_eeo_autofill"], (d) => {
    eeoBox.checked = d && d.spotapply_eeo_autofill === true;
  });
  eeoBox.addEventListener("change", () => {
    chrome.storage.local.set({ spotapply_eeo_autofill: eeoBox.checked });
  });
}

// ── Diagnostics ──────────────────────────────────────────────────────────────
// One click puts a full picture of THIS page on the clipboard: host, whether
// it's a recognised ATS, session state, and every form field with the signals
// the matcher saw and who filled it. No field values — only lengths and a
// redacted shape — so it's safe to paste into a chat or a bug report.
document.getElementById("btn-diag")?.addEventListener("click", () => {
  const btn = document.getElementById("btn-diag");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ Collecting…";
  const done = (msg, type) => {
    const el = document.getElementById("diag-status");
    el.textContent = msg;
    el.className = "status " + type;
    el.style.display = "block";
    btn.disabled = false;
    btn.textContent = label;
  };

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) return done("No active tab.", "err");
    chrome.storage.local.get(
      ["spotapply_fill_pack", "spotapply_sessions"],
      (store) => {
        chrome.tabs.sendMessage(tab.id, { type: "DIAGNOSTIC_REPORT" }, (page) => {
          if (chrome.runtime.lastError || !page) {
            return done("No content script on this tab — open the job form first.", "err");
          }
          const sess = (store.spotapply_sessions || {})[tab.id] || null;
          const pack = sess ? sess.pack : null;
          const ts = sess ? sess.ts : 0;
          const report = {
            extensionVersion: chrome.runtime.getManifest().version,
            session: {
              hasPack: !!pack,
              boundToThisTab: !!sess,
              job: pack ? `${pack.job_title || "?"} @ ${pack.company || "?"}` : null,
              appId: pack ? pack.app_id : null,
              submitAttempted: !!(sess && sess.attempt),
              sessionAgeSec: ts ? Math.round((Date.now() - ts) / 1000) : null,
              packHasResumeFields: pack ? { app_id: !!pack.app_id, token: !!pack.auth_token } : null,
            },
            page,
          };
          const text = JSON.stringify(report, null, 2);
          navigator.clipboard.writeText(text).then(
            () => done(`✅ Copied (${page.fieldCount} fields, ${page.filledCount} filled by SpotApply).`, "ok"),
            () => done("Couldn't copy — see the console for the report.", "err"),
          );
          console.log("[SpotApply] Diagnostic report:\n" + text);
        });
      },
    );
  });
});

// ── LinkedIn profile import ──────────────────────────────────────────────────
// Show the import card only when the active tab is the user's own LinkedIn
// profile (linkedin.com/in/...). Clicking it asks the content script to read the
// already-rendered profile and POST it to the legal import endpoint.
function showLinkedInStatus(msg, type) {
  const el = document.getElementById("linkedin-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "status " + type;
  el.style.display = "block";
}

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const url = (tabs && tabs[0] && tabs[0].url) || "";
  if (/^https?:\/\/[^/]*linkedin\.com\/in\//i.test(url)) {
    const sec = document.getElementById("linkedin-section");
    if (sec) sec.style.display = "block";
  }
});

document.getElementById("btn-linkedin")?.addEventListener("click", () => {
  const btn = document.getElementById("btn-linkedin");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ Importing...";

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (!tabs || !tabs[0]) {
      showLinkedInStatus("No active tab.", "err");
      btn.disabled = false;
      btn.textContent = label;
      return;
    }
    chrome.tabs.sendMessage(tabs[0].id, { type: "IMPORT_LINKEDIN" }, (res) => {
      btn.disabled = false;
      btn.textContent = label;
      if (chrome.runtime.lastError || !res) {
        showLinkedInStatus("Open your LinkedIn profile (linkedin.com/in/...) and try again.", "err");
      } else if (res.ok) {
        showLinkedInStatus("✅ Imported! Open your SpotApply dashboard for suggestions.", "ok");
      } else {
        showLinkedInStatus("⚠️ " + (res.error || "Import failed."), "err");
      }
    });
  });
});
