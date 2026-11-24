import { useState } from "react";
import { apiPost } from "../lib/api";
import type { BoardCard } from "./types";

/** Hosted mode: the trade happens on the venue; this records it in the journal. */
export function LogTrade({ card, onClose }: { card: BoardCard; onClose: () => void }) {
  const [price, setPrice] = useState(card.price);
  const [contracts, setContracts] = useState(10);
  const [note, setNote] = useState("");
  const [state, setState] = useState<"idle" | "saved" | string>("idle");
  const save = async () => {
    try { await apiPost("/journal", { card, price, contracts, note }); setState("saved"); }
    catch (e) { setState((e as Error).message); }
  };
  return (
    <div className="modal" role="dialog" aria-label="Log this trade">
      <div className="modal-card">
        <h3>Log this trade?</h3>
        <p className="muted">Kalshi opened in a new tab. If you took it, log it so the journal can grade your reasons.</p>
        <label>Price paid <input type="number" step="0.01" min={0.01} max={0.99} value={price} onChange={(e) => setPrice(+e.target.value)} /></label>
        <label>Contracts <input type="number" min={1} value={contracts} onChange={(e) => setContracts(Math.max(1, +e.target.value || 1))} /></label>
        <label>Note <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="why you took it" /></label>
        {state === "saved" ? <p className="good">Logged.</p> : state !== "idle" && <p className="bad">{state}</p>}
        <div className="row">
          <button onClick={onClose}>{state === "saved" ? "Close" : "Skip"}</button>
          {state !== "saved" && <button className="primary" onClick={save}>Log trade</button>}
        </div>
      </div>
    </div>
  );
}
