#!/usr/bin/env python3
"""Kentucky Lottery scratch-off tracker.

Reads the "available games" page (current prizes remaining per game), reads each
game's rules PDF (tickets ordered + original prize counts), and estimates live
odds and expected return per $1 for every game.

    python main.py                  # normal daily run
    python main.py --check          # connectivity test: fetch page + one PDF, print sizes
    python main.py --html-file page.html --pdf-dir pdfs/ --no-notify   # offline test
                                      (pdfs/ holds files named <game number>.pdf)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import pdfplumber
from bs4 import BeautifulSoup

LISTING_URL = "https://www.kylottery.com/apps/scratch_offs/available_games.html"
USER_AGENT = os.environ.get("USER_AGENT", "personal-scratchoff-tracker/1.0")
RULES_MAX_AGE_DAYS = 7          # re-download a game's rules PDF after this long
COMMON_TIER_MIN_WINNERS = 10_000  # tiers this common are used to estimate tickets sold

LABELS = {
    "start date": "start_date",
    "game end / last day to purchase": "game_end",
    "last date to claim": "last_claim",
    "value": "price",
    "top prize": "top_prize",
    "overall odds": "overall_odds",
    "game #": "game_number",
}

TIER_RE = re.compile(r"^\$\s?([\d,]+(?:\.\d{1,2})?)\s+([\d,]+)\s+([\d,]+(?:\.\d+)?)\s*$")
ANNUITY_RE = re.compile(
    r"^\$\s?([\d,]+)\s+(?:a|per)\s+(?:year|week)\b.*?\s([\d,]+)\s+([\d,]+(?:\.\d+)?)\s*$", re.I
)
CASH_RE = re.compile(r"or\s+\$\s?([\d,]+)\s+if\s+cash\s+option", re.I)


# --------------------------------------------------------------------------- helpers
def num(text):
    """'$1,234**' -> 1234 ; '7.50' -> 7.5 ; no digits -> None."""
    s = re.sub(r"[^\d.]", "", text or "")
    if not s or s == ".":
        return None
    f = float(s)
    return int(f) if f.is_integer() else f


def http_get(url, tries=3):
    import httpx  # imported lazily so offline tests don't need it

    last = None
    for i in range(tries):
        try:
            r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)
            r.raise_for_status()
            return r.content
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed for {url}: {last}")


def clean_url(base, href):
    """Resolve a relative link and percent-encode unsafe characters without double-encoding."""
    u = urlsplit(urljoin(base, href.strip()))
    path = quote(unquote(u.path), safe="/$'()+,;=@!~*-._&")
    return urlunsplit((u.scheme, u.netloc, path, u.query, ""))


# --------------------------------------------------------------------------- listing page
def parse_listing(html, base=LISTING_URL):
    soup = BeautifulSoup(html, "html.parser")
    games = []
    for panel in soup.select("div.panel.panel-info"):
        title_el = panel.select_one("h4.panel-title")
        if not title_el:
            continue
        title = " ".join(title_el.get_text(" ", strip=True).split())
        g = {"title": title, "remaining": {}}

        for span in panel.find_all("span"):
            text = span.get_text(" ", strip=True).replace("\xa0", " ").strip()
            if text.lower().startswith("prizes remaining as of"):
                b = span.find("b")
                g["as_of"] = b.get_text(strip=True) if b else None
                continue
            key = LABELS.get(text.rstrip(":").strip().lower())
            if key:
                b = span.find_next_sibling("b")
                if b:
                    g[key] = b.get_text(strip=True)

        for tr in panel.select("table tbody tr"):
            p = tr.select_one('td[title="Prize Amount"]')
            c = tr.select_one('td[title="Prizes Remaining"]')
            if p and c:
                amount, count = num(p.get_text()), num(c.get_text())
                if amount is not None and count is not None:
                    g["remaining"][amount] = g["remaining"].get(amount, 0) + count

        for a in panel.find_all("a", href=True):
            if a.get_text(strip=True).lower().startswith("download"):
                g["rules_url"] = clean_url(base, a["href"])
                break

        number = num(g.get("game_number")) or num((re.search(r"-\s*(\d+)\s*$", title) or [None, None])[1])
        if number is None:
            continue
        g["game_number"] = str(int(number))
        g["name"] = re.sub(r"\s*-\s*\d+\s*$", "", title)
        g["price"] = num(g.get("price"))
        m = re.search(r"1\s*:\s*([\d.,]+)", g.get("overall_odds", ""))
        g["overall_odds"] = num(m.group(1)) if m else None
        games.append(g)
    return games


# --------------------------------------------------------------------------- rules PDF
def parse_rules_text(text):
    out = {"tickets": None, "tiers": [], "fund": None, "payout_pct": None, "total_odds": None}

    m = re.search(r"approximately\s+([\d,]+)\s+tickets\s+ordered", text, re.I) or re.search(
        r"BASED ON\s+([\d,]+)\s+TICKETS\s+ORDERED", text, re.I
    )
    if m:
        out["tickets"] = num(m.group(1))
    m = re.search(r"TOTAL GAME ODDS\s+([\d.]+)", text, re.I)
    if m:
        out["total_odds"] = num(m.group(1))
    m = re.search(r"PRIZE FUND\s+\$([\d,]+)\s*\(([\d.]+)%\)", text, re.I)
    if m:
        out["fund"], out["payout_pct"] = num(m.group(1)), num(m.group(2))

    lines = [ln.strip() for ln in text.splitlines()]
    end = next((i for i, ln in enumerate(lines) if re.match(r"TOTAL GAME ODDS", ln, re.I)), len(lines))
    start = next((i for i, ln in enumerate(lines[:end]) if "EXPECTED" in ln), 0)

    by_prize = {}

    def add(prize, winners, odds):
        if prize is None or winners is None:
            return
        if prize in by_prize:
            by_prize[prize]["winners"] += winners
            by_prize[prize]["odds"] = None
        else:
            by_prize[prize] = {"prize": prize, "winners": winners, "odds": odds}

    for i in range(start, end):
        line = lines[i]
        m = TIER_RE.match(line)
        if m:
            add(num(m.group(1)), num(m.group(2)), num(m.group(3)))
            continue
        m = ANNUITY_RE.match(line)
        if m:  # e.g. "$50,000 a year for 20 years 3 960,000.00" + "(or $862,000 if cash option...)"
            prize = num(m.group(1))
            for nxt in lines[i + 1 : i + 3]:
                c = CASH_RE.search(nxt)
                if c:
                    prize = num(c.group(1))
                    break
            add(prize, num(m.group(2)), num(m.group(3)))

    out["tiers"] = sorted(by_prize.values(), key=lambda t: -t["prize"])
    return out


def parse_rules_pdf(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    return parse_rules_text(text)


def get_rules(game, args, rules_dir):
    number, url = game["game_number"], game.get("rules_url")

    if args.pdf_dir:  # offline testing
        p = Path(args.pdf_dir) / f"{number}.pdf"
        return parse_rules_pdf(p.read_bytes()) if p.exists() else None
    if not url:
        return None

    cache = rules_dir / f"{number}.json"
    if cache.exists() and not args.refresh_rules:
        c = json.loads(cache.read_text())
        age_days = (time.time() - c.get("fetched_at", 0)) / 86400
        if c.get("url") == url and age_days < RULES_MAX_AGE_DAYS:
            return c["parsed"]

    data = http_get(url)
    parsed = parse_rules_pdf(data)
    if parsed["tickets"] and parsed["tiers"]:  # only cache successful parses
        rules_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {"url": url, "fetched_at": time.time(), "sha256": hashlib.sha256(data).hexdigest(), "parsed": parsed},
                indent=1,
            )
        )
    time.sleep(1.0)  # be polite to the server
    return parsed


# --------------------------------------------------------------------------- analysis
def analyze(game, rules):
    price = game.get("price")
    rem = dict(game.get("remaining") or {})
    res = {
        "game": game["game_number"],
        "name": game["name"],
        "price": price,
        "as_of": game.get("as_of"),
        "listed_odds": game.get("overall_odds"),
        "top_prize_listed": game.get("top_prize"),
        "basis": None,
        "reliable": False,
        "flags": [],
        "remaining": rem,
        "frac_left": None,
        "tickets_left": None,
        "live_odds_any": None,
        "ev_now": None,
        "ev_orig": None,
        "top_remaining": None,
        "top_original": None,
    }
    if not price or not rem:
        res["flags"].append("missing price or prize table on page")
        return res

    tiers = {t["prize"]: t for t in (rules or {}).get("tiers", [])}
    tickets = (rules or {}).get("tickets")

    # ---- fallback: no usable PDF -> rough estimate from the odds printed on the page
    if not tiers or not tickets:
        total_rem = sum(rem.values())
        odds = game.get("overall_odds")
        if odds and total_rem:
            left = total_rem * odds
            res.update(
                basis="listed_odds",
                tickets_left=left,
                live_odds_any=odds,
                ev_now=sum(a * c for a, c in rem.items()) / (left * price),
            )
            res["flags"].append("no usable rules PDF: rough estimate from listed overall odds")
        else:
            res["flags"].append("no usable rules PDF and no listed odds")
        return res

    res["basis"] = "rules_pdf"
    res["tickets_ordered"] = tickets
    hard, soft = [], []

    unknown = sorted(a for a in rem if a not in tiers)
    if unknown:
        hard.append(f"page prize tiers not found in PDF: {unknown}")
    absent = sorted(a for a in tiers if a not in rem)
    if absent:
        soft.append(f"PDF tiers missing from page (treated as 0 left): {absent}")
        for a in absent:
            rem[a] = 0
    over = sorted(a for a in rem if a in tiers and rem[a] > tiers[a]["winners"])
    if over:
        hard.append(f"remaining exceeds original for {over} (reorder? PDF counts out of date)")
    bad = [t["prize"] for t in tiers.values() if t["odds"] and abs(t["winners"] * t["odds"] - tickets) / tickets > 0.02]
    if bad:
        hard.append(f"PDF table inconsistent (winners x odds != tickets) for {bad}")

    orig_pool = sum(t["prize"] * t["winners"] for t in tiers.values())
    fund = rules.get("fund")
    if fund and abs(orig_pool - fund) / fund > 0.005:
        soft.append(f"prize pool from table (${orig_pool:,.0f}) differs from stated fund (${fund:,.0f})")

    # ---- how much of the game is left? pool the most numerous tiers (least noisy)
    common = [a for a in tiers if tiers[a]["winners"] >= COMMON_TIER_MIN_WINNERS and a in rem]
    if len(common) < 2:
        common = sorted((a for a in tiers if a in rem), key=lambda a: -tiers[a]["winners"])[:3]
    per_tier = {a: rem[a] / tiers[a]["winners"] for a in common}
    frac = sum(rem[a] for a in common) / sum(tiers[a]["winners"] for a in common)
    spread = max(per_tier.values()) - min(per_tier.values())
    if spread > 0.05:
        soft.append(f"common tiers disagree on % left (spread {spread:.0%}); estimate is rougher")

    tickets_left = tickets * frac
    total_rem = sum(rem.values())
    pool = sum(a * c for a, c in rem.items())
    top = max(tiers)

    res.update(
        frac_left=frac,
        tickets_left=tickets_left,
        live_odds_any=tickets_left / total_rem if total_rem else None,
        ev_now=pool / (tickets_left * price) if tickets_left else None,
        ev_orig=orig_pool / (tickets * price),
        top_prize_amount=top,
        top_remaining=rem.get(top, 0),
        top_original=tiers[top]["winners"],
        top_odds_now=(tickets_left / rem[top]) if rem.get(top) else None,
        tier_odds_now={str(a): round(tickets_left / c, 2) for a, c in sorted(rem.items(), reverse=True) if c},
        remaining_pool=pool,
    )
    res["flags"] = hard + soft
    res["reliable"] = not hard and res["ev_now"] is not None
    return res


# --------------------------------------------------------------------------- output
def fmt_line(i, r):
    return (
        f"{i}. {r['name']} #{r['game']} (${r['price']:g}): "
        f"{r['ev_now'] * 100:.1f}¢ back per $1 (orig {r['ev_orig'] * 100:.1f}¢), "
        f"{r['frac_left'] * 100:.0f}% left, top prizes {r['top_remaining']}/{r['top_original']}"
    )


def build_report(results):
    ok = sorted((r for r in results if r["reliable"]), key=lambda r: r["ev_now"], reverse=True)
    dates = Counter(r["as_of"] for r in results if r["as_of"])
    as_of = dates.most_common(1)[0][0] if dates else "unknown date"
    lines = [f"KY scratch-offs: prizes as of {as_of} (estimates, from claimed-prize data)", "", "Best estimated return per $1:"]
    lines += [fmt_line(i, r) for i, r in enumerate(ok[:5], 1)]
    movers = sorted(ok, key=lambda r: r["ev_now"] - r["ev_orig"], reverse=True)[:3]
    if movers:
        lines += ["", "Most improved vs. launch payout:"]
        lines += [
            f"- {r['name']} #{r['game']}: {(r['ev_now'] - r['ev_orig']) * 100:+.1f}¢ ({r['ev_now'] * 100:.1f}¢ now)"
            for r in movers
        ]
    skipped = len(results) - len(ok)
    lines += ["", f"{len(ok)} games ranked, {skipped} skipped or low-confidence (details in data/latest.json)."]
    return "\n".join(lines)


def save_outputs(results, data_dir, report):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "latest.json").write_text(json.dumps(results, indent=1, default=str))
    (data_dir / "report.txt").write_text(report)

    path = data_dir / "snapshots.csv"
    cols = [
        "run_utc", "as_of", "game", "name", "price", "basis", "reliable", "tickets_left", "frac_left",
        "ev_now", "ev_orig", "live_odds_any", "top_remaining", "top_original", "flags", "remaining_json",
    ]
    seen, is_new = set(), not path.exists()
    if not is_new:
        with path.open(newline="") as f:
            seen = {(r["as_of"], r["game"]) for r in csv.DictReader(f)}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if is_new:
            w.writeheader()
        for r in results:
            if (str(r["as_of"]), str(r["game"])) in seen:
                continue
            row = dict(r)
            row["run_utc"] = now
            row["flags"] = "; ".join(r["flags"])
            row["remaining_json"] = json.dumps({str(a): c for a, c in r["remaining"].items()})
            w.writerow(row)


def notify(text):
    url = os.environ.get("WEBHOOK_URL")
    if not url:
        print("(WEBHOOK_URL not set; skipping notification)")
        return
    import httpx

    try:
        httpx.post(url, json={"content": text[:1900]}, timeout=30).raise_for_status()
        print("Notification sent.")
    except Exception as e:  # noqa: BLE001
        print(f"Notification failed: {e}")


# --------------------------------------------------------------------------- commands
def check():
    html = http_get(LISTING_URL).decode("utf-8", "replace")
    games = parse_listing(html)
    with_pdf = [g for g in games if g.get("rules_url")]
    print(f"Listing page: {len(html):,} chars, {len(games)} games parsed, {len(with_pdf)} with a rules PDF link")
    if with_pdf:
        g = with_pdf[0]
        data = http_get(g["rules_url"])
        p = parse_rules_pdf(data)
        print(f"PDF for #{g['game_number']} {g['name']}: {len(data):,} bytes, tickets={p['tickets']}, tiers={len(p['tiers'])}")
    return 0 if games else 1


def run(args):
    data_dir = Path(args.data_dir)
    html = (
        Path(args.html_file).read_text(encoding="utf-8")
        if args.html_file
        else http_get(LISTING_URL).decode("utf-8", "replace")
    )
    games = parse_listing(html)
    print(f"Parsed {len(games)} games from the listing page.")
    if len(games) < args.min_games:
        print(f"ERROR: expected at least {args.min_games} games; the page layout may have changed.")
        return 1

    results, errors = [], []
    for g in games:
        try:
            rules = get_rules(g, args, data_dir / "rules")
        except Exception as e:  # noqa: BLE001
            rules = None
            errors.append(f"#{g['game_number']} rules: {e}")
        results.append(analyze(g, rules))

    report = build_report(results)
    print(report)
    for r in results:
        if r["flags"]:
            print(f"  [#{r['game']} {r['name']}] " + " | ".join(r["flags"]))
    for e in errors:
        print("  ERROR", e)

    save_outputs(results, data_dir, report)
    if not args.no_notify:
        notify(report)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="test connectivity and parsing, then exit")
    ap.add_argument("--html-file", help="use a saved listing page instead of fetching it")
    ap.add_argument("--pdf-dir", help="use local PDFs named <game number>.pdf instead of downloading")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--refresh-rules", action="store_true", help="ignore cached rules PDFs")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--min-games", type=int, default=10, help="fail if fewer games are parsed (layout-change alarm)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return check() if args.check else run(args)


if __name__ == "__main__":
    sys.exit(main())
