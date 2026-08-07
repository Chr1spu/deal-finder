// Sends the capture command to the content script on the active tab. The
// popup deliberately does no parsing and no network work of its own: the
// content script is the only thing with access to the page DOM, and keeping
// the split clean means one place to debug when a site changes its markup.

// Derived from the manifest rather than written out again, because the two
// disagreed: this list used to accept web.facebook.com and a bare depop.com,
// neither of which the manifest injects a content script into. On those the
// button enabled itself, sendMessage found no receiver, and the popup blamed
// the user with "Reload the page, then try again" forever. Same failure shape
// as the queue-name literal that outlived its constant: a second copy of a
// fact drifts from the first. There is now one copy.
function patternToRegExp(pattern) {
  const parts = pattern.match(/^(\*|https?):\/\/([^/]+)(\/.*)$/);
  if (!parts) return null;
  const [, scheme, host, path] = parts;
  const escape = (value) => value.replace(/[.+?^${}()|[\]\\]/g, "\\$&");
  const hostPart = host === "*" ? "[^/]+" : escape(host).replace(/^\\\*\\\./, "(?:[^/]+\\.)?");
  return new RegExp(
    "^" + (scheme === "*" ? "https?" : scheme) + ":\\/\\/" + hostPart +
    escape(path).replace(/\*/g, ".*")
  );
}

const SUPPORTED = (chrome.runtime.getManifest().content_scripts || [])
  .flatMap((script) => script.matches || [])
  .map(patternToRegExp)
  .filter(Boolean);

function isSupported(url) {
  return SUPPORTED.some((pattern) => pattern.test(url));
}

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
  if (!tab || !isSupported(tab.url || "")) {
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
