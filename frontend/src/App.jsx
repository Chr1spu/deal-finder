import { useEffect, useState } from "react";
import { api, getApiKey, setApiKey } from "./api";
import DealCard from "./DealCard";
import SavedSearches from "./SavedSearches";

const TABS = ["Deals", "Searches"];

export default function App() {
  const [tab, setTab] = useState("Deals");
  const [key, setKey] = useState(getApiKey());

  return (
    <div className="app">
      <header className="top">
        <h1>Deal Finder</h1>
        <nav>
          {TABS.map((t) => (
            <button key={t} className={tab === t ? "tab active" : "tab"} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </nav>
        {/* Reads work without a key; writes need one and the server refuses
            them outright when it has none configured either. */}
        <input
          className="apikey"
          type="password"
          value={key}
          placeholder="API key (needed to change searches)"
          onChange={(e) => { setKey(e.target.value); setApiKey(e.target.value); }}
        />
      </header>
      <main>{tab === "Deals" ? <Deals /> : <SavedSearches />}</main>
    </div>
  );
}

function Deals() {
  const [feed, setFeed] = useState(null);
  const [error, setError] = useState(null);
  const [minScore, setMinScore] = useState(0.2);
  const [minConf, setMinConf] = useState(0.3);

  useEffect(() => {
    setFeed(null);
    api
      .deals({ limit: 50, min_deal_score: minScore, min_confidence: minConf })
      .then(setFeed)
      .catch((e) => setError(e.message));
  }, [minScore, minConf]);

  if (error) return <p className="error">{error}</p>;
  if (!feed) return <p className="muted">Loading deals...</p>;

  return (
    <>
      <div className="filters">
        <label>
          Min discount
          <input type="range" min="0" max="0.8" step="0.05" value={minScore}
                 onChange={(e) => setMinScore(Number(e.target.value))} />
          <span className="muted">{(minScore * 100).toFixed(0)}%</span>
        </label>
        <label>
          Min confidence
          <input type="range" min="0" max="1" step="0.05" value={minConf}
                 onChange={(e) => setMinConf(Number(e.target.value))} />
          <span className="muted">{minConf.toFixed(2)}</span>
        </label>
      </div>

      {/* Both filters exist together on purpose: a large discount computed
          from two shaky comps is the single most likely thing to be wrong,
          and is exactly what an unfiltered "biggest discounts" list puts
          first. See docs/decisions/0014. */}
      <p className="note">{feed.note}</p>
      {feed.last_scan_at && (
        <p className="muted small">
          Last scan {new Date(feed.last_scan_at).toLocaleString()}. The feed is served from
          that scan, not recomputed per request: a full scan is thousands of vector queries.
        </p>
      )}

      {feed.deals.length === 0 ? (
        <p className="muted">
          No deals meet these thresholds. Lower them, or wait for the next scan.
        </p>
      ) : (
        feed.deals.map((d) => <DealCard key={d.listing_id} deal={d} />)
      )}
    </>
  );
}
