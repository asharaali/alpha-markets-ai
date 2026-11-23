import type { ReactNode } from "react";
import type { PanelStatus } from "../lib/api";

const hhmm = (epochSec: number) =>
  new Date(epochSec * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

export function Panel(props: {
  title: string; source: string; status: PanelStatus; asOf: number | null;
  error?: string; actions?: ReactNode; className?: string; children?: ReactNode;
}) {
  const { title, source, status, asOf, error, actions, className, children } = props;
  return (
    <section className={`panel ${className ?? ""}`} aria-label={title}>
      <header className="panel-head">
        <h2>{title}</h2>
        <span className={`chip chip-${status.toLowerCase()}`}>{status}</span>
        {actions}
      </header>
      <div className="panel-body">
        {status === "ERROR" ? <p className="panel-error">{error}</p>
          : status === "LOADING" ? <p className="panel-loading">Loading…</p>
          : children}
      </div>
      <footer className="panel-foot">
        <span>{source}</span>
        {asOf != null && <time>{hhmm(asOf)}</time>}
      </footer>
    </section>
  );
}
