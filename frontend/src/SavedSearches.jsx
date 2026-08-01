import { useEffect, useState } from "react";
import { api } from "./api";

/**
 * The keywords that decide what the system ever sees, and what they cost.
 *
 * The budget is shown next to the add box rather than tucked away, because
 * every enabled search costs one eBay call per ingest run forever, and the
 * API refuses (not warns) once the total would exceed the daily allowance.
 * Seeing the number next to the control is what makes the refusal
 * unsurprising when it happens. See docs/decisions/0016-saved-search-crud.md.
 */
export default function SavedSearches() {
  const [data, setData] = useState(null);
  const [keyword, setKeyword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.savedSearches().then(setData).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  async function add(e) {
    e.preventDefault();
    if (!keyword.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.createSearch(keyword);
      setKeyword("");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggle(search) {
    setError(null);
    try {
      await api.setSearchEnabled(search.id, !search.enabled);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  if (!data) return <p className="muted">{error || "Loading searches..."}</p>;

  const b = data.budget;
  const headroom = b.max_searches - b.enabled_searches;

  return (
    <section>
      <div className="budget">
        <strong>{b.enabled_searches}</strong> enabled searches ×{" "}
        {b.calls_per_search_per_day} calls/day = {b.ingest_calls_per_day} ingest, plus{" "}
        {b.check_calls_per_day} checking ={" "}
        <strong>{b.total_calls_per_day}</strong> of {b.daily_limit} daily eBay calls.
        <div className={headroom < 10 ? "warn" : "muted"}>
          Room for {headroom} more {headroom === 1 ? "search" : "searches"}.
        </div>
      </div>

      <form onSubmit={add} className="add-row">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="new keyword, e.g. steam deck oled"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !keyword.trim()}>Add</button>
      </form>

      {error && <p className="error">{error}</p>}

      <ul className="searches">
        {data.searches.map((s) => (
          <li key={s.id} className={s.enabled ? "" : "disabled"}>
            <label>
              <input type="checkbox" checked={s.enabled} onChange={() => toggle(s)} />
              <span>{s.keyword}</span>
            </label>
            {/* eBay reports how many results a query really has; ingestion
                only ever sees the first 200, so a large number means this
                search is being truncated and would benefit from narrowing. */}
            {s.last_result_total > 200 && (
              <span className="muted truncated" title="ingest only sees the first 200 results">
                {s.last_result_total.toLocaleString()} results, truncated
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
