// Shared content script. The per-site parser (sites/depop.js or
// sites/facebook.js) runs first and installs window.__undercutParse; this
// file handles everything site-agnostic: talking to the popup, posting to the
// local API, and rendering the result as an overlay on the page.
//
// Nothing runs until the user clicks the extension button. That is the whole
// premise of the push-based design (docs/decisions/0010-depop-is-push-based-now.md):
// a person choosing to capture what they are already looking at, not a crawler.

(function () {
  "use strict";

  const API = "http://localhost:8000";
  const OVERLAY_ID = "undercut-overlay";

  function money(value) {
    return value === null || value === undefined ? "n/a" : "$" + Number(value).toFixed(2);
  }

  function overlay() {
    let el = document.getElementById(OVERLAY_ID);
    if (el) return el;
    el = document.createElement("div");
    el.id = OVERLAY_ID;
    el.style.cssText = [
      "position:fixed", "top:16px", "right:16px", "z-index:2147483647",
      "width:340px", "max-height:70vh", "overflow:auto",
      "background:#14161a", "color:#e8e8ea", "border:1px solid #30343c",
      "border-radius:10px", "padding:14px 16px",
      "font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif",
      "box-shadow:0 8px 28px rgba(0,0,0,.45)",
    ].join(";");
    el.addEventListener("click", (e) => {
      if (e.target && e.target.dataset && e.target.dataset.close === "1") el.remove();
    });
    document.body.appendChild(el);
    return el;
  }

  function render(html) {
    overlay().innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">' +
      '<strong style="font-size:13px">Undercut</strong>' +
      '<span data-close="1" style="cursor:pointer;opacity:.6;padding:0 4px">&times;</span>' +
      "</div>" + html;
  }

  function renderMatch(match) {
    if (!match) return render("<div>Captured, but no match data returned.</div>");

    if (!match.analysed) {
      return render(
        '<div style="margin-bottom:8px"><strong>' + escapeHtml(match.title) + "</strong></div>" +
        "<div>Captured at " + money(match.price) + ".</div>" +
        '<div style="opacity:.7;margin-top:8px">' + escapeHtml(match.note) + "</div>" +
        '<div style="margin-top:10px"><button id="df-retry" style="cursor:pointer;padding:5px 10px;' +
        'background:#2a2f38;color:#e8e8ea;border:1px solid #3a4049;border-radius:5px">' +
        "Check again</button></div>"
      );
    }

    const ctx = match.price_context;
    const rows = match.candidates.slice(0, 6).map(function (c) {
      return (
        '<div style="display:flex;justify-content:space-between;gap:10px;padding:4px 0;' +
        'border-top:1px solid #24272e">' +
        '<a href="' + escapeHtml(c.url) + '" target="_blank" rel="noopener" ' +
        'style="color:#8ab4f8;text-decoration:none;flex:1;overflow:hidden;' +
        'text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(c.title) + "</a>" +
        '<span style="opacity:.85;white-space:nowrap">' + money(c.total_cost || c.price) +
        ' <span style="opacity:.5">' + c.similarity.toFixed(2) + "</span></span>" +
        "</div>"
      );
    }).join("");

    // Deliberately shows the eBay median next to the captured price WITHOUT
    // computing a "% below market" figure. These are asking prices, not sold
    // prices, so a percentage would imply a precision the data does not have.
    // That number is stage 4's job, once sold history exists.
    render(
      '<div style="margin-bottom:6px"><strong>' + escapeHtml(match.title) + "</strong></div>" +
      '<div style="margin-bottom:10px">This listing: <strong>' + money(match.total_cost || match.price) +
      "</strong></div>" +
      (ctx
        ? '<div style="background:#1b1e24;border-radius:6px;padding:8px 10px;margin-bottom:10px">' +
          "<div>eBay asking, median: <strong>" +
          money(ctx.median_total_cost || ctx.median_price) + "</strong></div>" +
          '<div style="opacity:.7;font-size:12px">range ' + money(ctx.min_price) + " to " +
          money(ctx.max_price) + " across " + ctx.candidate_count + " candidates</div>" +
          "</div>"
        : "") +
      '<div style="opacity:.75;font-size:12px;margin-bottom:6px">matched by ' +
      escapeHtml(match.matched_by) + ", confidence " + match.confidence.toFixed(2) + "</div>" +
      rows +
      '<div style="opacity:.6;font-size:11px;margin-top:10px;line-height:1.4">' +
      escapeHtml(match.note) + "</div>"
    );
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value === null || value === undefined ? "" : String(value);
    return div.innerHTML;
  }

  function validate(payload) {
    if (!payload) return "the page parser returned nothing";
    if (!payload.source_id) return "could not find a listing id on this page";
    if (!payload.price) return "could not find a price on this page";
    if (!payload.title) return "could not find a title on this page";
    return null;
  }

  // Read from chrome.storage rather than hardcoding, so the repo never
  // contains the secret and the key is per-install.
  async function apiKey() {
    try {
      const stored = await chrome.storage.local.get("apiKey");
      return stored.apiKey || "";
    } catch (e) {
      return "";
    }
  }

  async function capture() {
    if (typeof window.__undercutParse !== "function") {
      return render("<div>No parser loaded for this page.</div>");
    }

    let payload;
    try {
      payload = window.__undercutParse();
    } catch (e) {
      return render("<div>Page parse failed: " + escapeHtml(e.message) + "</div>");
    }

    // Parsers break when sites change their markup, which for these two
    // sources is a matter of when, not if. Say so plainly rather than posting
    // a half-empty row the backend will reject with a 422.
    const problem = validate(payload);
    if (problem) {
      return render(
        "<div>Could not read this listing: " + escapeHtml(problem) + ".</div>" +
        '<div style="opacity:.6;font-size:11px;margin-top:8px">The page layout has probably ' +
        "changed. This is expected occasionally for unofficial page parsing.</div>"
      );
    }

    render("<div>Capturing and matching against eBay...</div>");

    try {
      const key = await apiKey();
      const resp = await fetch(API + "/capture", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-API-Key": key },
        body: JSON.stringify(payload),
      });
      if (resp.status === 401 || resp.status === 503) {
        // The two auth failures are worth distinguishing: one is a missing
        // key in the extension, the other is a server with none configured.
        const body = await resp.text();
        return render(
          "<div>" + (resp.status === 401
            ? "The API rejected this extension's key. Set it from the toolbar popup."
            : "The API has no API_KEY configured, so it refuses writes. Set API_KEY in .env.") +
          "</div>" +
          '<div style="opacity:.6;font-size:11px;margin-top:6px">' +
          escapeHtml(body.slice(0, 200)) + "</div>"
        );
      }
      if (!resp.ok) {
        const body = await resp.text();
        return render(
          "<div>API returned " + resp.status + ".</div>" +
          '<div style="opacity:.6;font-size:11px;margin-top:6px">' +
          escapeHtml(body.slice(0, 300)) + "</div>"
        );
      }
      const data = await resp.json();
      renderMatch(data.match);

      const retry = document.getElementById("df-retry");
      if (retry && data.listing_id) {
        retry.addEventListener("click", async function () {
          render("<div>Checking...</div>");
          const r = await fetch(API + "/capture/" + data.listing_id + "/match");
          renderMatch(await r.json());
        });
      }
    } catch (e) {
      render(
        "<div>Could not reach the Undercut API at " + API + ".</div>" +
        '<div style="opacity:.6;font-size:11px;margin-top:6px">Start it with: ' +
        "uv run uvicorn api.main:app</div>"
      );
    }
  }

  chrome.runtime.onMessage.addListener(function (message, _sender, sendResponse) {
    if (message && message.action === "capture") {
      capture();
      sendResponse({ started: true });
    }
    return true;
  });
})();
