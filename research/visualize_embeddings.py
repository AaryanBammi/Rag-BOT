"""Plot every chunk embedding in the Chroma index as an interactive 2-D map.

    python -m research.visualize_embeddings                     # t-SNE, writes research/embeddings.html
    python -m research.visualize_embeddings --method pca        # faster, but clusters overlap more
    python -m research.visualize_embeddings --query "exit load of HDFC Small Cap" --query "ELSS lock-in"

The 384-d vectors are projected to 2-D with t-SNE (or PCA), both written in numpy so no extra
packages are needed. The page is self-contained: colour by scheme / source type / chunk kind /
document, hover for the chunk text, click a point to list its nearest neighbours by cosine
similarity in the original space (what dense retrieval sees, not the 2-D distance). Each --query
is embedded with the configured model and its top-k chunks are ringed on the map.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
import webbrowser
from pathlib import Path

import chromadb
import numpy as np

from app.config import settings
from ingest.store import COLLECTION

ROOT = Path(__file__).resolve().parent
NEIGHBOURS = 8
SNIPPET_CHARS = 600


# --- projection -----------------------------------------------------------------------------

def pca(x: np.ndarray, dims: int = 2) -> np.ndarray:
    centred = x - x.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    return centred @ vt[:dims].T


def _affinities(x: np.ndarray, perplexity: float) -> np.ndarray:
    """Symmetric t-SNE input affinities, with a per-point bandwidth found by binary search."""
    n = len(x)
    sq = np.sum(x * x, axis=1)
    d = np.maximum(sq[:, None] + sq[None, :] - 2 * x @ x.T, 0)
    off_diag = ~np.eye(n, dtype=bool)
    d_min = np.where(off_diag, d, np.inf).min(axis=1, keepdims=True)
    d = np.where(off_diag, d - d_min, 0)  # shift by each row's nearest distance for stability
    target = np.log(perplexity)
    beta = np.ones(n)
    lo, hi = np.full(n, -np.inf), np.full(n, np.inf)
    for _ in range(64):
        p = np.exp(-d * beta[:, None]) * off_diag
        s = p.sum(axis=1)
        entropy = np.log(s) + beta * (d * p).sum(axis=1) / s
        if np.all(np.abs(entropy - target) < 1e-4):
            break
        too_flat = entropy > target  # bandwidth too wide -> raise beta
        lo = np.where(too_flat, beta, lo)
        hi = np.where(too_flat, hi, beta)
        beta = np.where(too_flat,
                        np.where(np.isinf(hi), beta * 2, (lo + hi) / 2),
                        np.where(np.isinf(lo), beta / 2, (lo + hi) / 2))
    p /= p.sum(axis=1, keepdims=True)
    p = (p + p.T) / (2 * n)
    return np.maximum(p, 1e-12).astype(np.float32)


def tsne(x: np.ndarray, perplexity: float = 30.0, iterations: int = 750, seed: int = 0) -> np.ndarray:
    """Exact t-SNE (van der Maaten & Hinton 2008) with early exaggeration, momentum and gains."""
    n = len(x)
    x = pca(x, min(50, x.shape[1])).astype(np.float32)  # standard pre-step: denoise and speed up
    p = _affinities(x, perplexity)
    rng = np.random.default_rng(seed)
    y = (rng.standard_normal((n, 2)) * 1e-4).astype(np.float32)
    velocity = np.zeros_like(y)
    gains = np.ones_like(y)
    lr = max(n / 12.0, 50.0)
    started = time.perf_counter()
    for it in range(iterations):
        exaggeration, momentum = (12.0, 0.5) if it < 250 else (1.0, 0.8)
        sq = np.sum(y * y, axis=1)
        num = 1.0 / (1.0 + sq[:, None] + sq[None, :] - 2 * y @ y.T)
        np.fill_diagonal(num, 0.0)
        q = np.maximum(num / num.sum(), 1e-12)
        pq = (exaggeration * p - q) * num
        grad = 4.0 * (pq.sum(axis=1)[:, None] * y - pq @ y)
        same_sign = np.sign(grad) == np.sign(velocity)
        gains = np.maximum(np.where(same_sign, gains * 0.8, gains + 0.2), 0.01)
        velocity = momentum * velocity - lr * gains * grad
        y += velocity
        y -= y.mean(axis=0)
        if (it + 1) % 125 == 0:
            kl = float(np.sum(p * np.log(p / q)))
            print(f"  t-SNE {it + 1}/{iterations}  KL={kl:.3f}  ({time.perf_counter() - started:.0f}s)", flush=True)
    return y


# --- data -----------------------------------------------------------------------------------

def load_chunks(index_dir: Path) -> tuple[list[str], np.ndarray, list[str], list[dict]]:
    client = chromadb.PersistentClient(path=str(index_dir))
    try:
        col = client.get_collection(COLLECTION, embedding_function=None)
    except Exception:  # noqa: BLE001
        raise SystemExit(f"No vector index at {index_dir}. Run `python -m ingest` first.")
    res = col.get(include=["embeddings", "documents", "metadatas"])
    order = sorted(range(len(res["ids"])),
                   key=lambda i: (res["metadatas"][i]["doc_id"], res["metadatas"][i]["chunk_index"]))
    ids = [res["ids"][i] for i in order]
    vectors = np.asarray([res["embeddings"][i] for i in order], dtype=np.float32)
    texts = [res["documents"][i] for i in order]
    metas = [res["metadatas"][i] for i in order]
    model = (col.metadata or {}).get("embedding_model", "?")
    print(f"Loaded {len(ids)} chunks, {vectors.shape[1]}-d, model {model}")
    return ids, vectors, texts, metas


def nearest(unit: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    sims = unit @ unit.T
    np.fill_diagonal(sims, -np.inf)
    idx = np.argpartition(-sims, k, axis=1)[:, :k]
    top = np.take_along_axis(sims, idx, axis=1)
    order = np.argsort(-top, axis=1)
    return np.take_along_axis(idx, order, axis=1), np.take_along_axis(top, order, axis=1)


def query_hits(unit: np.ndarray, queries: list[str], k: int) -> list[dict]:
    if not queries:
        return []
    from ingest.embed import get_embedder

    embedder = get_embedder()
    out = []
    for q in queries:
        v = np.asarray(embedder.embed_query(q), dtype=np.float32)
        sims = unit @ (v / np.linalg.norm(v))
        top = np.argsort(-sims)[:k]
        out.append({"query": q, "hits": [[int(i), round(float(sims[i]), 3)] for i in top]})
        print(f"  query {q!r}: top similarity {sims[top[0]]:.3f}")
    return out


# --- page -----------------------------------------------------------------------------------

def build_page(points: dict, title: str) -> str:
    data = json.dumps(points, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return PAGE.replace("__TITLE__", html.escape(title)).replace("__DATA__", data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research.visualize_embeddings")
    parser.add_argument("--method", choices=["tsne", "pca"], default="tsne")
    parser.add_argument("--perplexity", type=float, default=30.0)
    parser.add_argument("--iterations", type=int, default=750)
    parser.add_argument("--query", action="append", default=[], help="highlight this query's nearest chunks (repeatable)")
    parser.add_argument("--top-k", type=int, default=settings.retrieval_top_k)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "embeddings.html")
    parser.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    args = parser.parse_args(argv)

    index_dir = (args.data_dir or settings.data_dir) / "index"
    ids, vectors, texts, metas = load_chunks(index_dir)
    unit = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    print(f"Projecting with {args.method}…")
    started = time.perf_counter()
    xy = pca(unit) if args.method == "pca" else tsne(unit, args.perplexity, args.iterations)
    print(f"  done in {time.perf_counter() - started:.1f}s")
    nb_idx, nb_sim = nearest(unit, NEIGHBOURS)
    queries = query_hits(unit, args.query, args.top_k)

    points = {
        "method": args.method,
        "x": [round(float(v), 3) for v in xy[:, 0]],
        "y": [round(float(v), 3) for v in xy[:, 1]],
        "id": ids,
        "doc": [m["doc_id"] for m in metas],
        "scheme": [m.get("scheme_id") or "general" for m in metas],
        "source": [m.get("source_type", "") for m in metas],
        "kind": [m.get("kind", "text") for m in metas],
        "page": [m.get("page", 0) for m in metas],
        "as_of": [m.get("as_of") or "" for m in metas],
        "text": [t[:SNIPPET_CHARS] + ("…" if len(t) > SNIPPET_CHARS else "") for t in texts],
        "nb": nb_idx.tolist(),
        "nbSim": np.round(nb_sim, 3).tolist(),
        "queries": queries,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_page(points, f"Chunk embeddings ({args.method}, {len(ids)} chunks)"), encoding="utf-8")
    print(f"Wrote {args.out}")
    if not args.no_open:
        webbrowser.open(args.out.resolve().as_uri())
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --bg:#0f1318; --panel:#171c23; --text:#e3e8ef; --muted:#93a0b0; --border:#2a323d; --accent:#6aa5ff; }
  * { box-sizing:border-box; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  .app { display:grid; grid-template-columns:1fr 360px; height:100%; }
  .stage { position:relative; overflow:hidden; }
  canvas { display:block; width:100%; height:100%; cursor:grab; }
  canvas.dragging { cursor:grabbing; }
  .toolbar { position:absolute; top:12px; left:12px; right:12px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .toolbar select, .toolbar input, .toolbar button { background:var(--panel); color:var(--text); border:1px solid var(--border);
    border-radius:8px; padding:6px 10px; font:inherit; }
  .toolbar input { width:220px; }
  .toolbar .count { color:var(--muted); }
  .legend { position:absolute; left:12px; bottom:12px; max-height:45%; overflow:auto; background:rgba(23,28,35,.92);
    border:1px solid var(--border); border-radius:8px; padding:8px 10px; }
  .legend div { display:flex; align-items:center; gap:8px; padding:2px 0; cursor:pointer; user-select:none; }
  .legend div.off { opacity:.35; }
  .legend .swatch { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
  .legend .n { color:var(--muted); margin-left:auto; padding-left:10px; }
  .tooltip { position:absolute; pointer-events:none; max-width:360px; background:var(--panel); border:1px solid var(--border);
    border-radius:8px; padding:8px 10px; font-size:12.5px; white-space:pre-wrap; display:none; }
  aside { border-left:1px solid var(--border); background:var(--panel); overflow:auto; padding:14px; }
  aside h2 { font-size:15px; margin:0 0 8px; }
  aside h3 { font-size:13px; margin:16px 0 6px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .meta { color:var(--muted); font-size:12.5px; }
  .chunk-text { white-space:pre-wrap; background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:8px; font-size:12.5px; }
  .item { padding:6px 8px; border-radius:6px; cursor:pointer; border:1px solid transparent; }
  .item:hover { border-color:var(--border); background:var(--bg); }
  .item .sim { color:var(--accent); font-variant-numeric:tabular-nums; margin-right:6px; }
  .item .snip { color:var(--muted); font-size:12px; display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .hint { color:var(--muted); }
  @media (max-width: 800px) { .app { grid-template-columns:1fr; grid-template-rows:60% 40%; } aside { border-left:0; border-top:1px solid var(--border); } }
</style>
</head>
<body>
<div class="app">
  <div class="stage" id="stage">
    <canvas id="canvas"></canvas>
    <div class="toolbar">
      <label>Colour by <select id="colorBy">
        <option value="scheme">scheme</option><option value="source">source type</option>
        <option value="kind">chunk kind</option><option value="doc">document</option>
      </select></label>
      <input id="search" type="search" placeholder="Search chunk text…">
      <select id="querySel" hidden></select>
      <button id="reset" type="button">Reset view</button>
      <span class="count" id="count"></span>
    </div>
    <div class="legend" id="legend"></div>
    <div class="tooltip" id="tooltip"></div>
  </div>
  <aside id="panel"></aside>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const N = D.x.length;
const $ = (id) => document.getElementById(id);
const canvas = $("canvas"), ctx = canvas.getContext("2d");
const PALETTE = ["#6aa5ff","#ff8a65","#7ed957","#f6c445","#c792ea","#4dd0e1","#ff6f91","#a3be8c","#ffb86c","#8be9fd",
                 "#e06c75","#98c379","#d19a66","#61afef","#c678dd","#56b6c2","#f78c6c","#bb80b3","#82aaff","#addb67"];
let colorBy = "scheme", hidden = new Set(), search = "", matches = null, selected = -1, hover = -1, activeQuery = -1;
let view = { scale: 1, ox: 0, oy: 0 };

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

function categories() {
  const counts = new Map();
  for (const v of D[colorBy]) counts.set(v, (counts.get(v) || 0) + 1);
  const keys = [...counts.keys()].sort();
  const color = new Map(keys.map((k, i) => [k, i < PALETTE.length ? PALETTE[i] : `hsl(${(i * 137.5) % 360} 65% 62%)`]));
  return { keys, counts, color };
}
let cats = categories();

// world <-> screen
const xs = D.x, ys = D.y;
const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
function fit() {
  const w = canvas.clientWidth, h = canvas.clientHeight, pad = 60;
  const s = Math.min((w - 2 * pad) / (maxX - minX || 1), (h - 2 * pad) / (maxY - minY || 1));
  view = { scale: s, ox: w / 2 - s * (minX + maxX) / 2, oy: h / 2 + s * (minY + maxY) / 2 };
}
const sx = (i) => view.ox + xs[i] * view.scale;
const sy = (i) => view.oy - ys[i] * view.scale;

function visible(i) { return !hidden.has(D[colorBy][i]); }

function draw() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const dimAll = matches !== null || selected >= 0 || activeQuery >= 0;
  const focus = new Set();
  if (selected >= 0) { focus.add(selected); D.nb[selected].forEach((j) => focus.add(j)); }
  const qhits = activeQuery >= 0 ? new Set(D.queries[activeQuery].hits.map((h) => h[0])) : null;
  const r = Math.max(2, Math.min(5, 2 + view.scale / 40));
  // pass 1: dimmed points, pass 2: highlighted points on top
  for (const pass of [0, 1]) {
    for (let i = 0; i < N; i++) {
      if (!visible(i)) continue;
      const lit = !dimAll || (matches && matches.has(i)) || focus.has(i) || (qhits && qhits.has(i));
      if ((pass === 1) !== lit) continue;
      ctx.globalAlpha = lit ? 0.95 : 0.12;
      ctx.fillStyle = cats.color.get(D[colorBy][i]);
      ctx.beginPath();
      const big = D.kind[i] !== "text" ? 1.8 : 1;
      ctx.arc(sx(i), sy(i), r * big, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  ctx.globalAlpha = 1;
  if (selected >= 0) {
    ctx.strokeStyle = "rgba(255,255,255,.35)"; ctx.lineWidth = 1;
    for (const j of D.nb[selected]) { ctx.beginPath(); ctx.moveTo(sx(selected), sy(selected)); ctx.lineTo(sx(j), sy(j)); ctx.stroke(); }
    ring(selected, "#ffffff", r + 5, 2.5);
  }
  if (qhits) D.queries[activeQuery].hits.forEach(([j], rank) => ring(j, rank === 0 ? "#ffffff" : "#f6c445", r + 4, 2));
  if (hover >= 0) ring(hover, "#ffffff", r + 3, 1.5);
}
function ring(i, color, radius, width) {
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath(); ctx.arc(sx(i), sy(i), radius, 0, Math.PI * 2); ctx.stroke();
}

function renderLegend() {
  const box = $("legend"); box.replaceChildren();
  for (const k of cats.keys) {
    const row = el("div", hidden.has(k) ? "off" : "");
    const sw = el("span", "swatch"); sw.style.background = cats.color.get(k);
    row.append(sw, el("span", "", k), el("span", "n", String(cats.counts.get(k))));
    row.title = "Click to hide/show, double-click to show only this";
    row.addEventListener("click", () => { hidden.has(k) ? hidden.delete(k) : hidden.add(k); renderLegend(); draw(); });
    row.addEventListener("dblclick", () => { hidden = new Set(cats.keys.filter((x) => x !== k)); renderLegend(); draw(); });
    box.append(row);
  }
  const shown = [...Array(N).keys()].filter(visible).length;
  $("count").textContent = `${shown} / ${N} chunks · ${D.method}` + (matches ? ` · ${matches.size} match` : "");
}

function describe(i) {
  return `${D.id[i]}\n${D.source[i]} · ${D.scheme[i]} · ${D.kind[i]}` + (D.page[i] ? ` · p${D.page[i]}` : "") + (D.as_of[i] ? ` · as of ${D.as_of[i]}` : "");
}

function chunkItem(i, sim) {
  const it = el("div", "item");
  if (sim !== undefined) it.append(el("span", "sim", sim.toFixed(3)));
  it.append(el("span", "", D.id[i]), el("span", "snip", D.text[i]));
  it.addEventListener("click", () => select(i));
  return it;
}

function renderPanel() {
  const p = $("panel"); p.replaceChildren();
  if (activeQuery >= 0) {
    const q = D.queries[activeQuery];
    p.append(el("h2", "", `Query: ${q.query}`), el("p", "meta", `Top ${q.hits.length} chunks by cosine similarity (dense retrieval only; the app also uses BM25 + RRF).`));
    q.hits.forEach(([j, s]) => p.append(chunkItem(j, s)));
  }
  if (selected >= 0) {
    const i = selected;
    p.append(el("h2", "", D.id[i]), el("p", "meta", describe(i).split("\n")[1]), el("div", "chunk-text", D.text[i]));
    p.append(el("h3", "", "Nearest neighbours (384-d cosine)"));
    D.nb[i].forEach((j, k) => p.append(chunkItem(j, D.nbSim[i][k])));
  }
  if (activeQuery < 0 && selected < 0) {
    p.append(el("h2", "", "Chunk embedding map"),
      el("p", "hint", "Each dot is one chunk. Hover for details, click to see its nearest neighbours in the original embedding space. Scroll to zoom, drag to pan. Larger dots are fact cards."),
      el("p", "hint", "2-D distance is only approximate: t-SNE keeps local neighbourhoods, not global distances, so trust the neighbour list over how far apart clusters look."));
  }
}

function select(i) { selected = i; renderPanel(); draw(); }

function pick(mx, my) {
  let best = -1, bestD = 100;
  for (let i = 0; i < N; i++) {
    if (!visible(i)) continue;
    const dx = sx(i) - mx, dy = sy(i) - my, d = dx * dx + dy * dy;
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

// interaction
let drag = null;
canvas.addEventListener("mousedown", (e) => { drag = { x: e.offsetX, y: e.offsetY, ox: view.ox, oy: view.oy, moved: false }; });
window.addEventListener("mouseup", (e) => {
  if (drag && !drag.moved && e.target === canvas) { const i = pick(e.offsetX, e.offsetY); select(i >= 0 ? i : -1); }
  drag = null; canvas.classList.remove("dragging");
});
canvas.addEventListener("mousemove", (e) => {
  if (drag) {
    const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) { drag.moved = true; canvas.classList.add("dragging"); }
    view.ox = drag.ox + dx; view.oy = drag.oy + dy; $("tooltip").style.display = "none"; draw(); return;
  }
  const i = pick(e.offsetX, e.offsetY);
  if (i !== hover) { hover = i; draw(); }
  const tip = $("tooltip");
  if (i < 0) { tip.style.display = "none"; return; }
  tip.textContent = describe(i) + "\n\n" + D.text[i].slice(0, 280) + (D.text[i].length > 280 ? "…" : "");
  tip.style.display = "block";
  const sw = canvas.clientWidth, x = e.offsetX + 14;
  tip.style.left = (x + 360 > sw ? e.offsetX - 374 : x) + "px";
  tip.style.top = Math.min(e.offsetY + 14, canvas.clientHeight - tip.offsetHeight - 8) + "px";
});
canvas.addEventListener("mouseleave", () => { hover = -1; $("tooltip").style.display = "none"; draw(); });
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const k = Math.exp(-e.deltaY * 0.0015);
  view.ox = e.offsetX - (e.offsetX - view.ox) * k; view.oy = e.offsetY - (e.offsetY - view.oy) * k; view.scale *= k; draw();
}, { passive: false });
$("colorBy").addEventListener("change", (e) => { colorBy = e.target.value; hidden = new Set(); cats = categories(); renderLegend(); draw(); });
$("search").addEventListener("input", (e) => {
  search = e.target.value.trim().toLowerCase();
  matches = search ? new Set([...Array(N).keys()].filter((i) => D.text[i].toLowerCase().includes(search) || D.id[i].toLowerCase().includes(search))) : null;
  renderLegend(); draw();
});
$("reset").addEventListener("click", () => { fit(); selected = -1; renderPanel(); draw(); });
if (D.queries.length) {
  const qs = $("querySel"); qs.hidden = false;
  qs.append(el("option", "", "No query"));
  D.queries.forEach((q, i) => { const o = el("option", "", q.query); o.value = String(i); qs.append(o); });
  qs.addEventListener("change", () => { activeQuery = qs.value === "No query" ? -1 : Number(qs.value); renderPanel(); draw(); });
  qs.value = "0"; activeQuery = 0;
}
window.addEventListener("resize", () => { fit(); draw(); });
fit(); renderLegend(); renderPanel(); draw();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
