import { useEffect, useState } from "react";
import { api } from "./api";

const money = (v) =>
  v === null || v === undefined
    ? "n/a"
    : `$${Number(v).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

/**
 * Listings being followed individually, over time.
 *
 * The deal feed and this view answer different questions, and the difference
 * is why both exist. The feed is a snapshot rebuilt from scratch by every
 * scan, so a listing that stops being a bargain silently leaves it. This is
 * the axis the feed cannot have: one listing, kept until the user says
 * otherwise, including after it has ended.
 *
 * The column that matters is the change since it was watched. Every other
 * number in this app is measured against comps, which are themselves asking
 * prices on listings that left the market for unknown reasons. This one
 * compares a listing against its own earlier price, so it is the only figure
 * here that does not depend on the estimate being right.
 */
export default function Watchlist() {
  const [feed, setFeed] = useState(null);
  const [error, setError] = useState(null);
  const [includeEnded, setIncludeEnded] = useState(true);

  const load = () =>
    api
      .watchlist({ include_ended: includeEnded })
      .then((data) => {
        setFeed(data);
        setError(null);
      })
      .catch((e) => setError(e.message));

  useEffect(() => {
    setFeed(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [includeEnded]);

  if (error) return <p className="error">{error}</p>;
  if (!feed) return <p className="muted">Loading watchlist...</p>;

  return (
    <>
      <div className="filters">
        <label>
          <input
            type="checkbox"
            checked={includeEnded}
            onChange={(e) => setIncludeEnded(e.target.checked)}
          />
          Show ended listings
        </label>
        <span className="muted">
          {feed.active_count} active, {feed.ended_count} ended
        </span>
      </div>

      <p className="note">{feed.note}</p>

      {feed.items.length === 0 ? (
        <p className="muted">
          Nothing watched yet. Open a deal and use Watch to keep following it after it
          leaves the feed.
        </p>
      ) : (
        feed.items.map((item) => (
          <WatchCard key={item.listing_id} item={item} onChange={load} />
        ))
      )}
    </>
  );
}

function WatchCard({ item, onChange }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(item.note || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const ended = item.status !== "active";
  const change = item.price_change_since_added;
  const dropped = change < -0.001;

  const act = (fn) => {
    setBusy(true);
    setError(null);
    fn()
      .then(onChange)
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <article className={ended ? "card card-ended" : "card"}>
      <header className="card-head" onClick={() => setOpen(!open)}>
        <div className={`score ${dropped ? "score-high" : ""}`}>
          {change === 0 ? "flat" : `${change > 0 ? "+" : ""}${(change * 100).toFixed(0)}%`}
          <span className="score-label">since watched</span>
        </div>
        <div className="card-main">
          <h3>{item.title}</h3>
          <div className="figures">
            <strong>{money(item.total_cost ?? item.current_price)}</strong>
            <span className="muted">was {money(item.price_when_added)}</span>
            {ended ? (
              <span className="warn">
                {item.status === "likely_sold" ? "likely sold" : item.status}
                {item.sale_confidence !== null &&
                  ` (sale confidence ${item.sale_confidence.toFixed(2)})`}
              </span>
            ) : (
              <span className="muted">active</span>
            )}
            {item.note && <span className="muted">{item.note}</span>}
          </div>
        </div>
        <span className={open ? "chevron open" : "chevron"} aria-hidden="true" />
      </header>

      {open && (
        <div className="card-body">
          <a href={item.url} target="_blank" rel="noopener noreferrer" className="link">
            Open on {item.source} &rarr;
          </a>

          {/* Ordinal, like everywhere else these two appear, and shown
              separately because ADR 0007 exists to keep them apart: whether a
              sale happened, and whether the price is what was paid. */}
          {ended && (
            <p className="muted small">
              Left the market{" "}
              {item.missing_since && new Date(item.missing_since).toLocaleDateString()}. eBay
              does not say whether a listing sold, expired or was withdrawn, so
              sale confidence {item.sale_confidence?.toFixed(2) ?? "n/a"} is an inference and
              price confidence {item.price_confidence?.toFixed(2) ?? "n/a"} says how much the
              recorded price is worth trusting.
            </p>
          )}

          {item.history.length > 1 ? (
            <PriceChart points={item.history} />
          ) : (
            <p className="muted small">
              No price changes recorded yet. Observations are written only when the price
              actually moves, so one point means it has held since it was first seen.
            </p>
          )}

          <div className="filters">
            <label>
              Note
              <input
                type="text"
                value={note}
                placeholder="why you are watching this"
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
            <button
              className="tab"
              disabled={busy || note === (item.note || "")}
              onClick={() => act(() => api.setWatchNote(item.listing_id, note || null))}
            >
              Save note
            </button>
            <button
              className="tab"
              disabled={busy}
              onClick={() => act(() => api.unwatch(item.listing_id))}
            >
              Unwatch
            </button>
          </div>
          {error && <p className="error">{error}</p>}
        </div>
      )}
    </article>
  );
}

function PriceChart({ points }) {
  const values = points.map((p) => p.price + (p.shipping_cost || 0));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const w = 100;
  const h = 28;

  // Stepped, not interpolated, for the same reason as the deal card's chart:
  // observations are written only when the price moved, so a straight line
  // between two of them would draw a gradual drift that never happened.
  let d = "";
  values.forEach((value, i) => {
    const x = (i / Math.max(1, values.length - 1)) * w;
    const y = h - ((value - min) / span) * h;
    d += i === 0 ? `M ${x} ${y}` : ` H ${x} V ${y}`;
  });

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-label="price history">
        <path
          d={d}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.2"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="chart-labels muted">
        <span>{money(values[0])}</span>
        <span>{values.length} price observations</span>
        <span>{money(values[values.length - 1])}</span>
      </div>
    </div>
  );
}
