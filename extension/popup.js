// Sends the capture command to the content script on the active tab. The
// popup deliberately does no parsing and no network work of its own: the
// content script is the only thing with access to the page DOM, and keeping
// the split clean means one place to debug when a site changes its markup.

const SUPPORTED = /^https:\/\/(www\.)?(depop\.com\/products\/|(www|web)\.facebook\.com\/marketplace\/item\/)/;

const button = document.getElementById("capture");
const status = document.getElementById("status");
const apiKeyInput = document.getElementById("apiKey");

// Stored per-install in chrome.storage rather than committed, so the repo
// never contains the secret. Writes fail closed server-side when no key is
// configured there either (docs/decisions/0017-api-key-auth.md).
chrome.storage.local.get("apiKey", (stored) => {
  if (stored.apiKey) apiKeyInput.value = stored.apiKey;
});
apiKeyInput.addEventListener("change", () => {
  chrome.storage.local.set({ apiKey: apiKeyInput.value.trim() });
});

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const tab = tabs[0];
  if (!tab || !SUPPORTED.test(tab.url || "")) {
    button.disabled = true;
    status.textContent =
      "Open a Depop or Facebook Marketplace listing page, then click here.";
    return;
  }

  button.addEventListener("click", () => {
    chrome.tabs.sendMessage(tab.id, { action: "capture" }, () => {
      // A missing receiver means the content script isn't loaded, usually
      // because the page was already open when the extension was installed.
      if (chrome.runtime.lastError) {
        status.textContent = "Reload the page, then try again.";
        return;
      }
      window.close();
    });
  });
});
