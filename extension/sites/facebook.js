// Facebook Marketplace page parser.
//
// Same contract as sites/depop.js: read the listing the user already has open,
// never fetch anything. Facebook's ToS prohibits scraping and it is login-
// walled with active bot detection, which is precisely why this runs as an
// extension in the user's own session and why no server in this project ever
// requests a Facebook page (docs/decisions/0001-multi-source-connector-strategy.md).
//
// Facebook is a harder parse than Depop: markup is obfuscated and there is no
// JSON-LD. The most durable handle is the hydration payload Facebook embeds in
// script tags, so that is tried first, with visible-text heuristics as the
// fallback.

(function () {
  "use strict";

  function parsePrice(text) {
    if (!text) return null;
    const cleaned = String(text).replace(/,/g, "");
    if (/free/i.test(cleaned)) return null; // "Free" is not a price we can value
    const match = cleaned.match(/(\d+(?:\.\d{1,2})?)/);
    return match ? parseFloat(match[1]) : null;
  }

  function idFromUrl() {
    const match = window.location.pathname.match(/\/marketplace\/item\/(\d+)/);
    return match ? match[1] : null;
  }

  // Facebook ships state in <script type="application/json"> blobs. Rather
  // than depend on a path through them (which changes constantly), search all
  // of them for an object that looks like a marketplace listing.
  function fromHydration() {
    const scripts = document.querySelectorAll('script[type="application/json"]');
    for (const script of scripts) {
      const text = script.textContent || "";
      if (text.indexOf("marketplace_listing_title") === -1) continue;
      try {
        const found = search(JSON.parse(text), 0);
        if (found) return found;
      } catch (e) {
        // Malformed or truncated blob; try the next one.
      }
    }
    return null;
  }

  function search(node, depth) {
    if (!node || typeof node !== "object" || depth > 12) return null;
    if (node.marketplace_listing_title) return node;
    for (const key of Object.keys(node)) {
      const found = search(node[key], depth + 1);
      if (found) return found;
    }
    return null;
  }

  function collectImages(listing) {
    const urls = [];
    if (listing) {
      const primary =
        listing.primary_listing_photo &&
        listing.primary_listing_photo.image &&
        listing.primary_listing_photo.image.uri;
      if (primary) urls.push(primary);
      const photos = listing.listing_photos || [];
      for (const photo of photos) {
        const uri = photo && photo.image && photo.image.uri;
        if (uri) urls.push(uri);
      }
    }
    if (!urls.length) {
      // Marketplace photos are served from scontent/fbcdn hosts. Filter by
      // size to skip avatars, reaction icons and other chrome.
      document.querySelectorAll("img").forEach((img) => {
        if (/fbcdn|scontent/.test(img.src) && img.naturalWidth >= 250) urls.push(img.src);
      });
    }
    return [...new Set(urls)].slice(0, 6);
  }

  window.__undercutParse = function () {
    const listing = fromHydration();

    const heading = document.querySelector('h1, [role="main"] span[dir="auto"]');
    const title =
      (listing && listing.marketplace_listing_title) ||
      (heading && heading.textContent) ||
      document.title.replace(/ \| Facebook.*$/, "");

    const price =
      (listing &&
        listing.listing_price &&
        (parsePrice(listing.listing_price.amount) ||
          parsePrice(listing.listing_price.formatted_amount))) ||
      parsePrice(
        (Array.from(document.querySelectorAll('span[dir="auto"]')).find((s) =>
          /^[$£€]\s?[\d,]+/.test((s.textContent || "").trim())
        ) || {}).textContent
      );

    const location =
      (listing &&
        listing.location_text &&
        (listing.location_text.text || listing.location_text)) ||
      null;

    return {
      source: "facebook",
      source_id: (listing && String(listing.id)) || idFromUrl(),
      title: (title || "").trim().slice(0, 300),
      price: price,
      currency:
        (listing && listing.listing_price && listing.listing_price.currency) || "USD",
      url: window.location.origin + window.location.pathname,
      images: collectImages(listing),
      description:
        (listing &&
          listing.redacted_description &&
          (listing.redacted_description.text || listing.redacted_description)) ||
        null,
      condition: (listing && listing.attribute_data_condition) || null,
      size: null,
      brand: null,
      location: typeof location === "string" ? location : null,
      seller:
        (listing && listing.marketplace_listing_seller && listing.marketplace_listing_seller.name) ||
        null,
      // Marketplace is local pickup by default, so zero is the honest value
      // rather than unknown. This is the asymmetry that makes Listing.total_cost
      // matter: an eBay comp at 500 plus 30 delivery is worth 530 delivered,
      // so a 450 local cash listing beats a naive price-to-price comparison.
      shipping_cost: 0.0,
    };
  };
})();
