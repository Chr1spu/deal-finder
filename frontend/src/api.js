// All API access in one place, so the auth rules live somewhere findable
// rather than being rediscovered at each call site.
//
// Reads need no key; writes need X-API-Key and fail CLOSED server-side when
// none is configured there (docs/decisions/0017-api-key-auth.md). The key is
// held in localStorage rather than bundled, so the repo never contains it.

const BASE = "/api";
const KEY_STORAGE = "dealFinderApiKey";

export function getApiKey() {
  try {
    return localStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

export function setApiKey(value) {
  try {
    localStorage.setItem(KEY_STORAGE, value.trim());
  } catch {
    /* private browsing, or storage disabled: the app still works read-only */
  }
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method && options.method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-API-Key"] = getApiKey();
  }

  const response = await fetch(BASE + path, { ...options, headers });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      // FastAPI's detail is a string for simple errors and an object for the
      // structured ones (the quota refusal carries its arithmetic there).
      if (body.detail) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : body.detail.error || JSON.stringify(body.detail);
      }
    } catch {
      /* non-JSON error body; the status line is what we have */
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

export const api = {
  deals: (params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request(`/deals${query ? `?${query}` : ""}`);
  },
  valueListing: (id) => request(`/deals/${id}`),
  priceHistory: (id) => request(`/listings/${id}/prices`),
  savedSearches: () => request("/saved-searches"),
  createSearch: (keyword) =>
    request("/saved-searches", { method: "POST", body: JSON.stringify({ keyword }) }),
  setSearchEnabled: (id, enabled) =>
    request(`/saved-searches/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) }),
  deleteSearch: (id) => request(`/saved-searches/${id}`, { method: "DELETE" }),
};
