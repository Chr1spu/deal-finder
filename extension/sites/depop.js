// Depop page parser.
//
// Reads the listing the user is currently viewing. Runs only on
// depop.com/products/*, in the user's own logged-in session, and only when
// they click capture. Nothing here fetches a page: the DOM is already loaded
// because the user navigated to it. That distinction is the whole reason this
// is an extension rather than a server-side connector, since Depop returns 403
// to every server-side request (docs/decisions/0010-depop-is-push-based-now.md).
//
// Prefers JSON-LD and Next.js hydration data over CSS selectors wherever
// possible. Class names on these sites are generated and change without
// notice, while the structured data a site emits for search engines is
// comparatively stable and is what it wants crawlers to read anyway.

(function () {
  "use strict";

  function parsePrice(text) {
    if (!text) return null;
    // Strips currency symbols, thousands separators and any "was/now" prefix.
    const match = String(text).replace(/,/g, "").match(/(\d+(?:\.\d{1,2})?)/);
    return match ? parseFloat(match[1]) : null;
  }

  function jsonLd() {
    const blocks = document.querySelectorAll('script[type="application/ld+json"]');
    for (const block of blocks) {
      try {
        const parsed = JSON.parse(block.textContent);
        const items = Array.isArray(parsed) ? parsed : [parsed];
        for (const item of items) {
          if (item && (item["@type"] === "Product" || item.offers)) return item;
        }
      } catch (e) {
        // A malformed block is not worth failing the capture over; fall
        // through to the DOM path below.
      }
    }
    return null;
  }

  function nextData() {
    const el = document.getElementById("__NEXT_DATA__");
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return null;
    }
  }

  // Walks the hydration blob looking for the product object, since its exact
  // path under props.pageProps changes between Depop releases. Searching by
  // shape rather than by path is what makes this survive a refactor.
  function findProduct(node, depth) {
    if (!node || typeof node !== "object" || depth > 8) return null;
    if (node.pictures && (node.price || node.priceAmount) && node.id) return node;
    for (const key of Object.keys(node)) {
      const found = findProduct(node[key], depth + 1);
      if (found) return found;
    }
    return null;
  }

  function idFromUrl() {
    const match = window.location.pathname.match(/\/products\/([^/?#]+)/);
    return match ? match[1] : null;
  }

  function collectImages(product, ld) {
    const urls = [];
    if (product && Array.isArray(product.pictures)) {
      for (const picture of product.pictures) {
        // Depop serves several renditions per picture; take the largest,
        // matching the s-l500 reasoning on the eBay side (ml/embeddings.py).
        const formats = Array.isArray(picture) ? picture : Object.values(picture || {});
        const best = formats
          .filter((f) => f && f.url)
          .sort((a, b) => (b.width || 0) - (a.width || 0))[0];
        if (best) urls.push(best.url);
      }
    }
    if (!urls.length && ld && ld.image) {
      const images = Array.isArray(ld.image) ? ld.image : [ld.image];
      urls.push(...images.filter(Boolean));
    }
    if (!urls.length) {
      document
        .querySelectorAll('img[src*="depop"]')
        .forEach((img) => img.src && urls.push(img.src));
    }
    return [...new Set(urls)].slice(0, 6);
  }

  function attribute(labels) {
    // Depop renders attributes as label/value pairs whose markup varies.
    // Matching on the visible label is more durable than on a class name.
    const nodes = document.querySelectorAll("p, span, div, li, dt, dd");
    for (const node of nodes) {
      const text = (node.textContent || "").trim();
      for (const label of labels) {
        if (text.toLowerCase() === label.toLowerCase()) {
          const value = node.nextElementSibling && node.nextElementSibling.textContent;
          if (value && value.trim()) return value.trim();
        }
        const inline = text.match(new RegExp(`^${label}\\s*[:\\-]\\s*(.+)$`, "i"));
        if (inline) return inline[1].trim();
      }
    }
    return null;
  }

  window.__undercutParse = function () {
    const ld = jsonLd();
    const data = nextData();
    const product = data ? findProduct(data.props || data, 0) : null;

    const offers = ld && (Array.isArray(ld.offers) ? ld.offers[0] : ld.offers);
    const price =
      (product && parsePrice(product.priceAmount || (product.price && product.price.priceAmount))) ||
      (offers && parsePrice(offers.price)) ||
      parsePrice(
        (document.querySelector('[data-testid*="price" i]') || {}).textContent
      );

    const sourceId = (product && String(product.id)) || idFromUrl();
    const title =
      (ld && ld.name) ||
      (product && (product.description || "").split("\n")[0]) ||
      (document.querySelector("h1") || {}).textContent ||
      document.title;

    return {
      source: "depop",
      source_id: sourceId,
      title: (title || "").trim().slice(0, 300),
      price: price,
      currency:
        (offers && offers.priceCurrency) ||
        (product && product.price && product.price.currencyName) ||
        "USD",
      url: window.location.origin + window.location.pathname,
      images: collectImages(product, ld),
      description: (product && product.description) || (ld && ld.description) || null,
      condition: attribute(["Condition"]),
      size: attribute(["Size"]),
      brand: attribute(["Brand"]) || (ld && ld.brand && (ld.brand.name || ld.brand)) || null,
      seller: (product && product.sellerId && String(product.sellerId)) || null,
      shipping_cost:
        (product && parsePrice(product.nationalShippingCost)) ||
        (offers && offers.shippingDetails && parsePrice(offers.shippingDetails.shippingRate &&
          offers.shippingDetails.shippingRate.value)) ||
        null,
    };
  };
})();
