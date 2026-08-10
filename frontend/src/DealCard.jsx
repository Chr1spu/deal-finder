import { useEffect, useState } from "react";
import { api } from "./api";

const money = (v) =>
  v === null || v === undefined ? "n/a" : `$${Number(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;

/**
 * One deal, expandable into the comps behind it.
 *
 * The expansion is the point of this whole view, not a detail. Every
 * correctness bug found in this project so far (accessories priced as
 * bargains, multi-variant from-prices, a graphics card comped against a
 * desktop PC, a trademark symbol breaking model matching) was caught by
 * reading the comps behind a deal score. Putting them one click away makes
 * that a habit rather than an investigation.
 */
export default function DealCard({ deal }) {
  const [open, setOpen] = useState(false);
  const [prices, setPrices] = useState(null);

  useEffect(() => {
    if (!open || prices !== null || !deal.listing_id) return;
    api.priceHistory(deal.listing_id).then(setPrices).catch(() => setPrices([]));
  }, [open, prices, deal.listing_id]);

  const asking = deal.total_cost ?? deal.asking_price;
  const spread = deal.signals?.spread_ratio;

  return (
    <article className="card">
      <header className="card-head" onClick={() => setOpen(!open)}>
        <div className={`score ${deal.deal_score >= 0.4 ? "score-high" : ""}`}>
          {(deal.deal_score * 100).toFixed(0)}%
          <span className="score-label">below comps</span>
        </div>
        <div className="card-main">
          <h3>{deal.title}</h3>
          <div className="figures">
            <strong>{money(asking)}</strong>
            <span className="muted">vs {money(deal.estimated_value)} estimated</span>
            <span className="muted">{deal.comp_count} comps</span>
            <Confidence value={deal.confidence} />
            {spread > 3 && <span className="warn">spread {spread.toFixed(1)}x</span>}
          </div>
        </div>
        <span className={open ? "chevron open" : "chevron"} aria-hidden="true" />
      </header>

      {open && (
        <div className="card-body">
          <div className="filters">
            <a href={deal.url} target="_blank" rel="noopener noreferrer" className="link">
              Open on {deal.source} &rarr;
            </a>
            <WatchButton listingId={deal.listing_id} />
          </div>

          <h4>Comps this estimate is built from</h4>
          <table className="comps">
            <tbody>
              {deal.comps.map((c) => (
                <tr key={c.listing_id}>
                  <td>
                    <a href={c.url} target="_blank" rel="noopener noreferrer">{c.title}</a>
                  </td>
                  <td className="num">{money(c.price)}</td>
                  {/* Both confidences shown, because they answer different
                      questions and ADR 0007 exists to keep them apart: did a
                      sale happen, and is the recorded price what was paid. */}
                  <td className="num muted" title="sale confidence">
                    {c.sale_confidence?.toFixed(2) ?? "-"}
                  </td>
                  <td className="num muted" title="price confidence">
                    {c.price_confidence?.toFixed(2) ?? "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {prices && prices.length > 1 && <PriceChart points={prices} />}

          <ul className="caveats">
            {deal.caveats.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}
    </article>
  );
}

/**
 * Watching is how a listing outlives the feed. The feed is rebuilt by every
 * scan, so a listing whose price rises out of the thresholds simply vanishes
 * from it, taking the reason anyone was interested with it.
 *
 * A 409 is reported as success, not as an error: the endpoint refuses a
 * second add so it cannot silently reset the price the listing was watched
 * at, and from here "it is already on your watchlist" is the outcome the
 * click was asking for.
 */
function WatchButton({ listingId }) {
  const [state, setState] = useState("idle");
  const [error, setError] = useState(null);

  if (!listingId) return null;

  const watch = () => {
    setState("busy");
    setError(null);
    api
      .watch(listingId)
      .then(() => setState("watched"))
      .catch((e) => {
        if (e.status === 409) {
          setState("watched");
        } else {
          setState("idle");
          setError(e.message);
        }
      });
  };

  return (
    <>
      <button className="tab" disabled={state !== "idle"} onClick={watch}>
        {state === "watched" ? "On watchlist" : state === "busy" ? "Adding..." : "Watch"}
      </button>
      {error && <span className="error">{error}</span>}
    </>
  );
}

function Confidence({ value }) {
  // Ordinal, never a probability (same convention as sale_confidence), so it
  // is drawn as a bar rather than printed as a percentage that would invite
  // reading it as one.
  return (
    <span className="conf" title={`confidence ${value.toFixed(2)} (ordinal, not a probability)`}>
      <span className="conf-fill" style={{ width: `${Math.round(value * 100)}%` }} />
    </span>
  );
}

function PriceChart({ points }) {
  const values = points.map((p) => p.total_cost ?? p.price);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const w = 100;
  const h = 28;

  // Stepped, not interpolated. Observations are written only when the price
  // actually moved, so a straight line between two points would draw a slow
  // drift that never happened: the price held, then jumped.
  let d = "";
  points.forEach((p, i) => {
    const x = (i / Math.max(1, points.length - 1)) * w;
    const y = h - (((p.total_cost ?? p.price) - min) / span) * h;
    d += i === 0 ? `M ${x} ${y}` : ` H ${x} V ${y}`;
  });

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-label="price history">
        <path d={d} fill="none" stroke="currentColor" strokeWidth="1.2" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="chart-labels muted">
        <span>{money(values[0])}</span>
        <span>{points.length} price observations</span>
        <span>{money(values[values.length - 1])}</span>
      </div>
    </div>
  );
}
