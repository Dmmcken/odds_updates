#!/usr/bin/env python3
"""Build docs/index.html (a self-contained, sortable web page) from data/latest.json.

    python build_site.py [--data-dir data] [--out-dir docs]

Also copies latest.csv / snapshots.csv into the output folder so the page can link to them.
"""
import argparse
import datetime as dt
import json
import re
import shutil
from collections import Counter
from pathlib import Path

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kentucky scratch-off odds tracker</title>
<meta name="description" content="Estimated live odds and payout for every Kentucky Lottery scratch-off game, updated daily.">
<style>
  :root {
    --paper: #f6f7f4; --card: #ffffff; --ink: #16202b; --muted: #5d6b79; --line: #dfe3e0;
    --blue: #1d3f7a; --grass: #1f7a52; --warn: #a4442a; --bar: #cfd9e8; --bar-fill: #1d3f7a;
    --focus: #1d3f7a;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #11171e; --card: #18212b; --ink: #e8edf2; --muted: #9aa9b8; --line: #2a3643;
      --blue: #8db0ee; --grass: #5fc496; --warn: #ee9a7e; --bar: #2a3a52; --bar-fill: #6f9be6;
      --focus: #8db0ee;
    }
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    padding: env(safe-area-inset-top, 0) 0 env(safe-area-inset-bottom, 0);
  }
  .wrap { max-width: 1040px; margin: 0 auto; padding: 28px 16px 48px; }
  h1 { font: 700 clamp(1.7rem, 4.5vw, 2.4rem)/1.15 Georgia, "Iowan Old Style", "Times New Roman", serif; margin: 0 0 6px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); margin: 0 0 22px; max-width: 62ch; }
  .controls { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; margin-bottom: 14px; }
  .controls input[type=search] {
    flex: 1 1 220px; min-width: 0; padding: 9px 12px; font: inherit; color: var(--ink);
    background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    font: inherit; font-size: .9rem; padding: 6px 11px; border-radius: 999px; cursor: pointer;
    border: 1px solid var(--line); background: var(--card); color: var(--ink);
  }
  .chip[aria-pressed=true] { background: var(--blue); border-color: var(--blue); color: var(--paper); }
  .toggle { font-size: .9rem; color: var(--muted); display: flex; gap: 6px; align-items: center; }
  :focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
  .scroller { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 10px; }
  table { border-collapse: collapse; width: 100%; min-width: 760px; font-variant-numeric: tabular-nums; }
  th, td { padding: 10px 12px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
  #tbl > thead th:nth-child(2), #tbl > tbody > tr.row > td:nth-child(2) { text-align: left; white-space: normal; min-width: 190px; }
  #tbl > thead th:first-child, #tbl > tbody > tr.row > td:first-child { text-align: center; width: 44px; color: var(--muted); }
  #tbl > thead th { position: sticky; top: 0; background: var(--card); z-index: 1; font-weight: 600; font-size: .85rem; color: var(--muted); }
  th button { all: unset; cursor: pointer; padding: 2px 0; }
  th button:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
  th[aria-sort=ascending] button::after { content: " \25B2"; font-size: .7em; }
  th[aria-sort=descending] button::after { content: " \25BC"; font-size: .7em; }
  tr.row { cursor: pointer; }
  tr.row:hover td { background: color-mix(in srgb, var(--blue) 6%, transparent); }
  .gname { font-weight: 600; }
  .gnum { color: var(--muted); font-size: .85rem; }
  .ret { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
  .meter { width: 70px; height: 8px; background: var(--bar); border-radius: 4px; overflow: hidden; flex: none; }
  .meter i { display: block; height: 100%; background: var(--bar-fill); }
  .up { color: var(--grass); } .down { color: var(--warn); }
  .badge { font-size: .75rem; color: var(--muted); border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; margin-left: 6px; }
  tr.detail > td { background: color-mix(in srgb, var(--blue) 4%, var(--card)); white-space: normal; text-align: left; }
  .dwrap { position: sticky; left: 12px; width: min(520px, calc(100vw - 56px)); }
  table.tiers { border-collapse: collapse; min-width: 0; width: 100%; margin: 4px 0; }
  table.tiers th, table.tiers td { padding: 3px 0 3px 14px; border: 0; position: static; background: none; text-align: right; font-size: .9rem; min-width: 0; white-space: nowrap; }
  table.tiers th:first-child, table.tiers td:first-child { text-align: left; padding-left: 0; width: auto; color: inherit; }
  table.tiers thead th { color: var(--muted); }
  .note { color: var(--muted); font-size: .85rem; margin: 6px 0 0; }
  .empty { padding: 28px; text-align: center; color: var(--muted); }
  details.how { margin: 22px 0 0; color: var(--muted); max-width: 70ch; }
  details.how summary { cursor: pointer; color: var(--ink); font-weight: 600; }
  footer { margin-top: 26px; color: var(--muted); font-size: .88rem; max-width: 72ch; }
  footer a { color: var(--blue); }
</style>
</head>
<body>
<div class="wrap">
  <h1>Kentucky scratch-off odds tracker</h1>
  <p class="sub">Estimated odds and average payout for every Kentucky Lottery scratch-off game, rebuilt daily from the lottery’s published prize data. Prizes as of <strong id="asof"></strong>.</p>

  <div class="controls">
    <input id="q" type="search" placeholder="Search by game name or number" aria-label="Search games">
    <div class="chips" id="chips" role="group" aria-label="Filter by ticket price"></div>
    <label class="toggle"><input type="checkbox" id="low"> Include rough estimates</label>
  </div>

  <div class="scroller"><table id="tbl">
    <thead><tr id="head"></tr></thead>
    <tbody id="body"></tbody>
  </table></div>
  <div id="count" class="note"></div>

  <details class="how">
    <summary>How the estimates work</summary>
    <p>The lottery publishes how many prizes of each size are left, and each game’s rules PDF says how many tickets were printed and how many prizes there were at the start. The most common prizes (such as the smallest ones) show what share of a game has already been sold. From that I estimate how many tickets are left, then divide the remaining prize money by the cost of those tickets to get the average payout per $1. Click any game to see its remaining prizes and the estimated odds of each.</p>
    <p>Games marked <em>rough</em> have no usable rules PDF, so their numbers come from the odds printed on the lottery’s page and are less reliable. Prizes only count as gone once they are claimed, so the true number of tickets left is a little lower than shown.</p>
  </details>

  <footer>
    <p>Estimates only, not advice or a guarantee. On average, players get back less than they spend on every game. If gambling stops being fun, call or text 1-800-GAMBLER.</p>
    <p>Data: <a href="https://www.kylottery.com/apps/scratch_offs/available_games.html">Kentucky Lottery</a>. Download today’s numbers as <a href="latest.csv">CSV</a> or the <a href="snapshots.csv">full history</a>. Page built <span id="built"></span>. Not affiliated with the Kentucky Lottery.</p>
  </footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
document.getElementById('asof').textContent = D.as_of || 'unknown date';
document.getElementById('built').textContent = D.built;

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const int = n => n == null ? '–' : Math.round(n).toLocaleString('en-US');
const cents = n => n == null ? '–' : n.toFixed(1) + '¢';
const dollars = n => '$' + Number(n).toLocaleString('en-US');
const signed = n => n == null ? '–' : (n > 0 ? '+' : n < 0 ? '\u2212' : '') + Math.abs(n).toFixed(1) + '¢';
const odds = n => n == null ? '–' : '1 in ' + (n >= 100 ? int(n) : n.toFixed(2).replace(/\.?0+$/, ''));

const COLS = [
  {k:'rank',  label:'#',               asc:true},
  {k:'name',  label:'Game',            asc:true},
  {k:'price', label:'Price',           asc:true},
  {k:'ev',    label:'Est. payout per $1', asc:false},
  {k:'chg',   label:'Vs. launch',      asc:false},
  {k:'left',  label:'Tickets left',    asc:false},
  {k:'top_l', label:'Top prizes left', asc:false},
  {k:'odds',  label:'Odds of any prize', asc:true},
];
let sortKey = 'rank', dir = 1, query = '', priceF = 'all', showLow = false;
const open = new Set();

const head = document.getElementById('head');
head.innerHTML = COLS.map(c => `<th scope="col" data-k="${c.k}"><button type="button">${esc(c.label)}</button></th>`).join('');
head.addEventListener('click', e => {
  const th = e.target.closest('th'); if (!th) return;
  const c = COLS.find(x => x.k === th.dataset.k);
  if (sortKey === c.k) dir = -dir; else { sortKey = c.k; dir = c.asc ? 1 : -1; }
  render();
});

const prices = [...new Set(D.games.map(g => g.price))].sort((a, b) => a - b);
const chips = document.getElementById('chips');
chips.innerHTML = ['all', ...prices].map(p => `<button class="chip" type="button" data-p="${p}" aria-pressed="${p === 'all'}">${p === 'all' ? 'All prices' : '$' + p}</button>`).join('');
chips.addEventListener('click', e => {
  const b = e.target.closest('.chip'); if (!b) return;
  priceF = b.dataset.p; render();
});
document.getElementById('q').addEventListener('input', e => { query = e.target.value.trim().toLowerCase(); render(); });
document.getElementById('low').addEventListener('change', e => { showLow = e.target.checked; render(); });

function cmp(a, b) {
  const A = a[sortKey], B = b[sortKey];
  if (A == null && B == null) return 0;
  if (A == null) return 1;
  if (B == null) return -1;
  return (typeof A === 'string' ? A.localeCompare(B) : A - B) * dir;
}

function detail(g) {
  const rows = g.tiers.map(t => `<tr><td>${dollars(t[0])}</td><td>${int(t[1])}</td><td>${odds(t[2])}</td></tr>`).join('');
  const notes = g.notes.length ? `<p class="note">${g.notes.map(esc).join('<br>')}</p>` : '';
  const topNote = g.top_note ? `<p class="note">${esc(g.top_note)}</p>` : '';
  return `<tr class="detail"><td colspan="8"><div class="dwrap">
    <table class="tiers"><thead><tr><th>Prize</th><th>Left</th><th>Est. odds</th></tr></thead><tbody>${rows}</tbody></table>
    ${topNote}${notes}</div></td></tr>`;
}

function render() {
  [...chips.children].forEach(b => b.setAttribute('aria-pressed', String(b.dataset.p === String(priceF))));
  [...head.children].forEach(th => th.setAttribute('aria-sort', th.dataset.k === sortKey ? (dir === 1 ? 'ascending' : 'descending') : 'none'));
  const list = D.games
    .filter(g => (showLow || g.ok) && (priceF === 'all' || String(g.price) === priceF) && (!query || (g.name + ' ' + g.game).toLowerCase().includes(query)))
    .sort(cmp);
  const body = document.getElementById('body');
  if (!list.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No games match. Clear the search or choose another price.</td></tr>';
  } else {
    body.innerHTML = list.map(g => {
      const isOpen = open.has(g.game);
      const chgCls = g.chg == null ? '' : g.chg > 0.05 ? 'up' : g.chg < -0.05 ? 'down' : '';
      const meter = g.ev == null ? '' : `<span class="meter" aria-hidden="true"><i style="width:${Math.min(100, g.ev)}%"></i></span>`;
      return `<tr class="row" tabindex="0" role="button" aria-expanded="${isOpen}" data-g="${esc(g.game)}">
        <td>${g.rank == null ? '–' : g.rank}</td>
        <td><span class="gname">${esc(g.name)}</span>${g.ok ? '' : '<span class="badge">rough</span>'}<br><span class="gnum">Game ${esc(g.game)}</span></td>
        <td>$${esc(g.price)}</td>
        <td><span class="ret">${meter}<strong>${cents(g.ev)}</strong></span></td>
        <td class="${chgCls}">${signed(g.chg)}</td>
        <td>${g.left == null ? '–' : g.left.toFixed(0) + '%'}</td>
        <td>${g.top_l == null ? '–' : g.top_l + ' of ' + g.top_o}</td>
        <td>${odds(g.odds)}</td></tr>${isOpen ? detail(g) : ''}`;
    }).join('');
  }
  document.getElementById('count').textContent = list.length + ' of ' + D.games.length + ' games shown. Click a game for its prize breakdown.';
}
document.getElementById('body').addEventListener('click', e => {
  const tr = e.target.closest('tr.row'); if (!tr) return;
  const id = tr.dataset.g; open.has(id) ? open.delete(id) : open.add(id); render();
  const again = document.querySelector(`tr.row[data-g="${CSS.escape(id)}"]`); if (again) again.focus();
});
document.getElementById('body').addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('tr.row')) { e.preventDefault(); e.target.click(); }
});
render();
</script>
</body>
</html>
"""


def top_note(r, tiers):
    """Explain when the advertised top prize differs from the cash amount in the prize table."""
    listed = re.sub(r"[^\d]", "", (r.get("top_prize_listed") or "").split(".")[0])
    if not listed or not tiers:
        return None
    top_cash = max(t[0] for t in tiers)
    if int(listed) != int(top_cash):
        return f"The lottery advertises a ${int(listed):,} top prize; the cash amount in this table is ${int(top_cash):,}."
    return None


def prepare(results):
    ok = sorted((r for r in results if r.get("reliable")), key=lambda r: r["ev_now"], reverse=True)
    rank = {r["game"]: i for i, r in enumerate(ok, 1)}
    games = []
    for r in results:
        if r.get("ev_now") is None:
            continue
        rem = {float(k): v for k, v in (r.get("remaining") or {}).items()}
        tier_odds = {float(k): v for k, v in (r.get("tier_odds_now") or {}).items()}
        tiers = [[a, rem[a], tier_odds.get(a)] for a in sorted(rem, reverse=True)]
        ev0 = r.get("ev_orig")
        games.append(
            {
                "game": str(r["game"]),
                "name": r["name"],
                "price": r["price"],
                "ok": bool(r.get("reliable")),
                "rank": rank.get(r["game"]),
                "ev": round(r["ev_now"] * 100, 2),
                "chg": round((r["ev_now"] - ev0) * 100, 2) if ev0 is not None else None,
                "left": round(r["frac_left"] * 100, 1) if r.get("frac_left") is not None else None,
                "top_l": r.get("top_remaining"),
                "top_o": r.get("top_original"),
                "odds": round(r["live_odds_any"], 2) if r.get("live_odds_any") else None,
                "top_note": top_note(r, tiers),
                "tiers": [[int(a) if a == int(a) else a, c, o] for a, c, o in tiers],
                "notes": r.get("flags") or [],
            }
        )
    dates = Counter(r["as_of"] for r in results if r.get("as_of"))
    as_of = dates.most_common(1)[0][0] if dates else None
    return {"as_of": as_of, "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "games": games}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out-dir", default="docs")
    args = ap.parse_args()
    data_dir, out_dir = Path(args.data_dir), Path(args.out_dir)

    results = json.loads((data_dir / "latest.json").read_text(encoding="utf-8"))
    payload = prepare(results)
    out_dir.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    (out_dir / "index.html").write_text(TEMPLATE.replace("__DATA__", blob), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")  # tell GitHub Pages to serve files as-is
    for name in ("latest.csv", "snapshots.csv"):
        if (data_dir / name).exists():
            shutil.copy(data_dir / name, out_dir / name)
    print(f"Wrote {out_dir / 'index.html'} with {len(payload['games'])} games.")


if __name__ == "__main__":
    main()
