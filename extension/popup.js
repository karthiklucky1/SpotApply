// popup.js — HirePath Extension Popup

const HIREPATH_URL = "https://hirepath.dev";

function showStatus(msg, type) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = "status " + type;
  el.style.display = "block";
}

// Load stored job pack
chrome.storage.local.get(["hirepath_fill_pack"], (data) => {
  if (data.hirepath_fill_pack) {
    const pack = data.hirepath_fill_pack;
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

  chrome.storage.local.get(["hirepath_fill_pack"], (data) => {
    if (!data.hirepath_fill_pack) {
      showStatus("No job loaded. Go to HirePath and click Fill with Extension.", "err");
      btn.disabled = false;
      btn.textContent = "⚡ Fill This Form Now";
      return;
    }
    chrome.runtime.sendMessage(
      { type: "FILL_JOB", payload: data.hirepath_fill_pack },
      (res) => {
        if (chrome.runtime.lastError || !res?.ok) {
          showStatus("Could not reach the form tab. Make sure the job form tab is active and open.", "err");
        } else {
          showStatus("✅ Done! Review every field carefully, then hit Submit.", "ok");
        }
        btn.disabled = false;
        btn.textContent = "⚡ Fill This Form Now";
      }
    );
  });
});

// Dashboard links
["btn-dash", "btn-dash2"].forEach((id) => {
  document.getElementById(id)?.addEventListener("click", () => {
    chrome.tabs.create({ url: HIREPATH_URL + "/dashboard" });
  });
});
