// HirePath Extension — Background Service Worker

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {

  // Dashboard sends fill pack → store it + message the active tab
  if (msg.type === "FILL_JOB") {
    chrome.storage.local.set({ hirepath_fill_pack: msg.payload, hirepath_auto_fill: false }, () => {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (!tabs[0]) { sendResponse({ ok: false, error: "No active tab" }); return; }
        chrome.tabs.sendMessage(tabs[0].id, { type: "DO_FILL", fillPack: msg.payload }, (res) => {
          sendResponse(res || { ok: true });
        });
      });
    });
    return true;
  }

  // Dashboard sends pack + URL → open new tab, auto-fill when loaded
  if (msg.type === "OPEN_AND_FILL") {
    chrome.storage.local.set({ hirepath_fill_pack: msg.payload, hirepath_auto_fill: true }, () => {
      chrome.tabs.create({ url: msg.payload.apply_url }, (tab) => {
        sendResponse({ ok: true, tabId: tab.id });
      });
    });
    return true;
  }

  if (msg.type === "PING") {
    sendResponse({ ok: true, version: chrome.runtime.getManifest().version });
  }
});

// When a new tab finishes loading, check if we have a pending auto-fill
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  chrome.storage.local.get(["hirepath_fill_pack", "hirepath_auto_fill"], (data) => {
    if (!data.hirepath_auto_fill || !data.hirepath_fill_pack) return;
    const pack = data.hirepath_fill_pack;
    if (!tab.url || !pack.apply_url) return;
    try {
      const tabHost = new URL(tab.url).hostname;
      const jobHost = new URL(pack.apply_url).hostname;
      if (tabHost !== jobHost) return;
    } catch (e) { return; }
    // Clear flag so it doesn't re-fire on refresh
    chrome.storage.local.set({ hirepath_auto_fill: false });
    setTimeout(() => {
      chrome.tabs.sendMessage(tabId, { type: "DO_FILL", fillPack: pack });
    }, 2500);
  });
});
