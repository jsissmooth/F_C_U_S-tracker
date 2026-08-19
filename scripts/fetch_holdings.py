import json
import os
import sys
import requests
import pandas as pd
from datetime import date
from io import StringIO
import pandas_market_calendars as mcal

CSV_URL  = "https://pinnacleetfs.com/feeds/Pinnacle.40T2.T2_Holdings.csv"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
TICKER   = "FCUS"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://pinnacleetfs.com/",
    "Accept": "text/csv,*/*",
}


def is_nyse_trading_day(d):
    nyse = mcal.get_calendar("NYSE")
    return not nyse.schedule(start_date=d.isoformat(), end_date=d.isoformat()).empty


def download_csv():
    resp = requests.get(CSV_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_holdings(csv_text):
    df = pd.read_csv(StringIO(csv_text))
    df.columns = [c.strip() for c in df.columns]
    records = []

    def safe_float(val):
        try:
            s = str(val).strip().replace("%", "").replace(",", "")
            v = float(s)
            return None if pd.isna(v) else v
        except (ValueError, TypeError):
            return None

    for _, row in df.iterrows():
        ticker = str(row.get("StockTicker", "")).strip()
        name   = str(row.get("SecurityName", "")).strip()
        cusip  = str(row.get("CUSIP", "")).strip()

        if not ticker or ticker.lower() == "nan":
            continue
        if name.lower() == "nan":
            name = ""
        if cusip.lower() == "nan":
            cusip = ""

        shares       = safe_float(row.get("Shares"))
        price        = safe_float(row.get("Price"))
        market_value = safe_float(row.get("MarketValue"))
        weight_raw   = safe_float(row.get("Weightings"))  # already stripped % above

        records.append({
            "ticker":       ticker,
            "name":         name,
            "identifier":   cusip,
            "quantity":     shares,
            "price":        price,
            "market_value": market_value,
            "pct_of_fund":  weight_raw,  # stored as 2.86 (already a percentage)
        })

    return records


def save_snapshot(records, today_str):
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {"date": today_str, "ticker": TICKER, "holdings": records}
    with open(os.path.join(DATA_DIR, "{}.json".format(today_str)), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(DATA_DIR, "latest.json"), "w") as f:
        json.dump(payload, f, indent=2)


def find_prior_snapshot(today_str):
    files = sorted(
        f for f in os.listdir(DATA_DIR)
        if f.endswith(".json") and f not in ("latest.json", "diff.json", "history.json")
    )
    prior = [f for f in files if f.replace(".json", "") < today_str]
    return os.path.join(DATA_DIR, prior[-1]) if prior else None


def compute_diff(today_records, prior_records, today_str, prior_date_str):
    today_map = {r["ticker"]: r for r in today_records}
    prior_map = {r["ticker"]: r for r in prior_records}
    all_keys  = sorted(set(today_map) | set(prior_map))
    rows = []
    for key in all_keys:
        t = today_map.get(key)
        p = prior_map.get(key)
        if t and p:
            q_today   = t["quantity"] or 0
            q_prior   = p["quantity"] or 0
            pct_today = t["pct_of_fund"] or 0
            pct_prior = p["pct_of_fund"] or 0
            qty_chg   = ((q_today - q_prior) / q_prior * 100) if q_prior != 0 else 0
            rows.append({
                "ticker":              t["ticker"],
                "name":                t.get("name") or p.get("name") or "",
                "identifier":          t.get("identifier") or "",
                "status":              "changed" if round(qty_chg, 6) != 0 else "unchanged",
                "quantity_today":      q_today,
                "quantity_prior":      q_prior,
                "quantity_pct_change": round(qty_chg, 4),
                "pct_of_fund_today":   pct_today,
                "pct_of_fund_prior":   pct_prior,
                "pct_of_fund_change":  round(pct_today - pct_prior, 4),
                "price_today":         t.get("price"),
                "market_value_today":  t.get("market_value"),
            })
        elif t:
            rows.append({
                "ticker": t["ticker"], "name": t.get("name") or "",
                "identifier": t.get("identifier") or "",
                "status": "added",
                "quantity_today": t["quantity"] or 0, "quantity_prior": None,
                "quantity_pct_change": None,
                "pct_of_fund_today": t["pct_of_fund"] or 0, "pct_of_fund_prior": None,
                "pct_of_fund_change": None,
                "price_today": t.get("price"), "market_value_today": t.get("market_value"),
            })
        else:
            rows.append({
                "ticker": p["ticker"], "name": p.get("name") or "",
                "identifier": p.get("identifier") or "",
                "status": "removed",
                "quantity_today": None, "quantity_prior": p["quantity"] or 0,
                "quantity_pct_change": None,
                "pct_of_fund_today": None, "pct_of_fund_prior": p["pct_of_fund"] or 0,
                "pct_of_fund_change": None,
                "price_today": None, "market_value_today": None,
            })
    return {"date": today_str, "ticker": TICKER, "prior_date": prior_date_str, "diff": rows}


def append_history(today_str, diff):
    history_path = os.path.join(DATA_DIR, "history.json")
    history = []
    if os.path.exists(history_path):
        with open(history_path) as f:
            history = json.load(f)
    entry = {"date": today_str, "prior_date": diff["prior_date"]}
    if entry not in history:
        history.append(entry)
        history.sort(key=lambda x: x["date"], reverse=True)
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)


def main():
    today_str = date.today().isoformat()
    today     = date.today()

    if not is_nyse_trading_day(today):
        print("{} is not a NYSE trading day -- skipping.".format(today_str), file=sys.stderr)
        sys.exit(0)

    print("Fetching FCUS holdings for {}...".format(today_str), file=sys.stderr)
    try:
        csv_text = download_csv()
        records  = parse_holdings(csv_text)
        if not records:
            print("No holdings parsed.", file=sys.stderr)
            sys.exit(1)
        print("{} holdings found.".format(len(records)), file=sys.stderr)
        save_snapshot(records, today_str)

        prior_path = find_prior_snapshot(today_str)
        if not prior_path:
            diff_rows = []
            for r in records:
                diff_rows.append({
                    "ticker":              r["ticker"],
                    "name":                r.get("name") or "",
                    "identifier":          r.get("identifier") or "",
                    "status":              "unchanged",
                    "quantity_today":      r["quantity"] or 0,
                    "quantity_prior":      r["quantity"] or 0,
                    "quantity_pct_change": 0,
                    "pct_of_fund_today":   r["pct_of_fund"] or 0,
                    "pct_of_fund_prior":   r["pct_of_fund"] or 0,
                    "pct_of_fund_change":  0,
                    "price_today":         r.get("price"),
                    "market_value_today":  r.get("market_value"),
                })
            diff = {"date": today_str, "ticker": TICKER, "prior_date": None, "diff": diff_rows}
        else:
            with open(prior_path) as f:
                prior_data = json.load(f)
            if prior_data["date"] == today_str:
                print("Already have data for {} -- skipping.".format(today_str), file=sys.stderr)
                sys.exit(0)
            diff = compute_diff(records, prior_data["holdings"], today_str, prior_data["date"])

        with open(os.path.join(DATA_DIR, "diff.json"), "w") as f:
            json.dump(diff, f, indent=2)

        append_history(today_str, diff)

        changed = sum(1 for r in diff["diff"] if r["status"] == "changed")
        added   = sum(1 for r in diff["diff"] if r["status"] == "added")
        removed = sum(1 for r in diff["diff"] if r["status"] == "removed")
        print("Done -- {} holdings | {} changed | {} added | {} removed".format(
            len(records), changed, added, removed), file=sys.stderr)

    except Exception as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
