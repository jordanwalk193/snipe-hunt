"""Your hunt in a Google Sheet: collection, picks, sold prices, offers, lowball snipes,
Gixen snipes, and a summary with your spending by month.

Optional. Set GOOGLE_SHEET_ID (the long id in the sheet's link) and
GOOGLE_SERVICE_ACCOUNT_FILE (a service-account key file, with the Google Sheets
API turned on in its project), and share the sheet with the service account's
email as an editor. `pip install google-auth` (the Docker image has it).

Each push rewrites the data tabs from the bot's own state. The Summary tab is
written once, as formulas over the data tabs, and is yours to change after that;
delete it and the next push writes a fresh one. Data is written RAW: a listing
title that starts with "=" stays text and can never become a formula. Push only:
nothing is read back from the sheet, and nothing in it changes the bot.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from . import config, db, inventory, sales

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SUMMARY, SOURCES = "Summary", "Data Sources"
MAX_SOLD = 5000
MONTHS = 24   # rows in the summary's spending-by-month table
GROW_SPARE = 500   # rows added past the data when a tab has to grow

# Collection columns, in the order the Summary's formulas expect them.
COLLECTION = (("id", "ID"), ("year", "Year"), ("set", "Set"), ("card", "Card"),
              ("parallel", "Parallel"), ("card_no", "Card no."), ("serial", "Serial"),
              ("rc", "RC"), ("auto", "Auto"), ("relic", "Relic"), ("graded", "Graded"),
              ("status", "Status"), ("price_paid", "Paid ($)"), ("order_date", "Order date"),
              ("seller", "Seller"), ("ebay_item_id", "eBay item"), ("notes", "Notes"))
HEADERS = {
    "Collection": [h for _, h in COLLECTION],
    "Picks": ["Tier", "Stars", "Card", "Type", "Price ($)", "Shipping ($)", "Max ($)",
              "Going rate ($)", "Rate from", "Under going rate (%)", "Ends", "Edge",
              "Seller", "Your snipe ($)", "eBay item", "Link"],
    "Sold": ["Sold on", "Title", "Price ($)", "Shipping ($)", "Total ($)", "Kind", "Bids",
             "Source", "eBay item", "Link"],
    "Offers": ["Title", "Asking ($)", "Shipping ($)", "Days up", "Offer ($)",
               "Going rate ($)", "Rate from", "Seller", "eBay item", "Link"],
    "Lowball": ["Title", "Bid now ($)", "Shipping ($)", "Bids", "Ends", "Going rate ($)",
                "Snipe ($)", "Dead hour", "Easy to miss", "In Gixen", "eBay item", "Link"],
    "Snipes": ["Title", "Your max ($)", "Bid now ($)", "Ends", "Verdict", "Going rate ($)",
               "Doubts", "eBay item", "Link"],
}
TABS = list(HEADERS)


class SheetsError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.get("GOOGLE_SHEET_ID") and config.get("GOOGLE_SERVICE_ACCOUNT_FILE"))


# ------------------------------------------------------------ rows
def _num(value) -> float | None:
    """A number for a money cell, or None (an empty cell) when there isn't one."""
    try:
        return round(float(str(value).replace("$", "").replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def _when(iso: str | None) -> str:
    """'2026-10-25 21:30': sorts as text, reads at a glance, never turned into a date."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(config.TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(iso)


def _blank(value):
    return "" if value is None else value


def _link(item_id) -> str:
    return f"https://www.ebay.com/itm/{item_id}" if item_id else ""


def collection_rows(rows: list[dict]) -> list[list]:
    return [[_num(r.get(k)) if k == "price_paid" else str(r.get(k) or "") for k, _ in COLLECTION]
            for r in rows]


def pick_rows(picks: list[dict]) -> list[list]:
    out = []
    for p in picks:
        kind = "Auction" if p.get("is_auction") else (
            "Best offer" if p.get("best_offer") else "Buy It Now")
        out.append([p.get("tier") or "", _blank(p.get("deal")),
                    p.get("card") or p.get("title") or "", kind, _blank(p.get("price")),
                    _blank(p.get("shipping")), _blank(p.get("max")),
                    _blank(p.get("value")), p.get("max_basis") or "", _blank(p.get("deal_pct")),
                    _when(p.get("end_time")), p.get("edge") or "", p.get("seller") or "",
                    _blank(p.get("snipe")), str(p["item_id"]), p.get("url") or _link(p["item_id"])])
    return out


def sold_rows(con, limit: int = MAX_SOLD) -> list[list]:
    out = []
    for r in con.execute("SELECT * FROM sales ORDER BY sold_at DESC LIMIT ?", (limit,)):
        r = dict(r)
        out.append([datetime.fromtimestamp(r["sold_at"], config.TZ).strftime("%Y-%m-%d"),
                    r.get("title") or "", r["price"], _blank(r.get("shipping")),
                    sales.delivered(r["price"], r.get("shipping")), r.get("kind") or "",
                    _blank(r.get("bids")), r.get("source") or "", str(r["item_id"]),
                    _link(r["item_id"])])
    return out


def offer_rows(offers: list[dict]) -> list[list]:
    return [[o.get("title") or "", o["price"], _blank(o.get("shipping")), _blank(o.get("days")),
             o["offer"], _blank(o.get("value")), o.get("basis") or "", o.get("seller") or "",
             str(o["item_id"]), o.get("url") or _link(o["item_id"])] for o in offers]


def lowball_rows(found: dict) -> list[list]:
    return [[t.get("title") or "", t["price"], _blank(t.get("shipping")), _blank(t.get("bids")),
             _when(t.get("end_time")), t["value"], t["snipe"], t.get("dead") or "",
             ", ".join(t.get("overlooked") or []), "yes" if t.get("sniped") else "",
             str(t["item_id"]), t.get("url") or _link(t["item_id"])]
            for t in found.get("targets") or []]


def snipe_rows(gix: dict | None) -> list[list]:
    out = []
    for s in (gix or {}).get("snipes") or []:
        check = s.get("check") or {}
        out.append([s.get("title") or "", _blank(s.get("max_bid")), _blank(s.get("current_bid")),
                    _when(s.get("end_time")), check.get("label") or "", _blank(check.get("value")),
                    "; ".join(d["text"] for d in s.get("doubts") or []), str(s["item_id"]),
                    s.get("url") or _link(s["item_id"])])
    return out


def tables(con, snap: dict) -> dict[str, list[list]]:
    """Every data tab, header row first."""
    _, rows = inventory.read()
    data = {"Collection": collection_rows(rows), "Picks": pick_rows(snap.get("picks") or []),
            "Sold": sold_rows(con), "Offers": offer_rows(snap.get("offers") or []),
            "Lowball": lowball_rows(snap.get("lowball") or {}),
            "Snipes": snipe_rows(snap.get("gixen"))}
    return {tab: [HEADERS[tab], *data[tab]] for tab in TABS}


def sources_rows(data: dict[str, list[list]], run_at: str) -> list[list]:
    """One row per data tab: where it came from and when (the Data Sources tab)."""
    what = {"Collection": ("inventory.csv", "the inventory file"),
            "Picks": ("picks", "the bot's live picks"),
            "Sold": ("sales", "sold prices the bot recorded (newest first)"),
            "Offers": ("offers", "Best Offer listings up 30+ days"),
            "Lowball": ("lowball", "cheap auctions of good cards"),
            "Snipes": ("Gixen", "your scheduled Gixen snipes, read-only")}
    head = ["Written to", "Source", "Object", "Obtained via", "Run at", "Row count", "Notes"]
    return [head] + [[f"{tab}!A1:{_col(len(HEADERS[tab]))}{len(rows)}", "Snipe Hunt",
                      what[tab][0], "Pushed by the bot (sheets.py)", run_at, len(rows) - 1,
                      what[tab][1]] for tab, rows in data.items()]


def _col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def summary_rows() -> list[list]:
    """The Summary tab: labels and formulas over the data tabs, written once."""
    paid, date, status = "Collection!$M$2:$M", "Collection!$N$2:$N", "Collection!$L$2:$L"
    rows = [
        ["Snipe Hunt", None, None, "Spending by month", None, None],
        ["Last pushed", f"=INDEX('{SOURCES}'!E2:E,1)", None, "Month", "Spent ($)", "Cards"],
        [None, None, None, None, None, None],
        ["Collection", None, None, None, None, None],
        ["Cards kept", f'=COUNTIFS(Collection!A2:A,"<>",{status},"<>Sold-Traded")'],
        ["Paid for them ($)", f'=SUMIFS({paid},{status},"<>Sold-Traded")'],
        ["Spent this month ($)", f'=SUMPRODUCT((LEFT({date},7)=TEXT(TODAY(),"yyyy-mm"))*{paid})'],
        [None],
        ["Hunting", None],
        ["Live picks", "=COUNTA(Picks!A2:A)"],
        ["Picks rated 4 stars or more", '=COUNTIF(Picks!B2:B,">=4")'],
        ["Offers to send", "=COUNTA(Offers!A2:A)"],
        ["Saved if every offer is taken ($)", "=SUM(Offers!B2:B)-SUM(Offers!E2:E)"],
        ["Lowball targets", "=COUNTA(Lowball!A2:A)"],
        ["Gixen snipes scheduled", "=COUNTA(Snipes!A2:A)"],
        ["Committed in Gixen ($)", "=SUM(Snipes!B2:B)"],
        ["Snipes with a doubt", '=COUNTIF(Snipes!G2:G,"?*")'],
        [None],
        ["Market", None],
        ["Sold prices on record", "=COUNTA(Sold!A2:A)"],
        ["Median sold total ($)", "=IF(COUNT(Sold!E2:E)=0,0,MEDIAN(Sold!E2:E))"],
    ]
    rows = [r + [None] * (6 - len(r)) for r in rows]
    rows += [[None] * 6 for _ in range(2 + MONTHS - len(rows))]
    # the month list fills itself from the order dates (oldest first, for the chart);
    # each row's total and count come from the month label beside it
    rows[2][3] = f'=IFERROR(SORT(UNIQUE(FILTER(LEFT({date},7),LEN({date})>=7))),"")'
    for r in range(3, 3 + MONTHS):   # sheet rows 3 to MONTHS + 2
        month = f"D{r}"
        rows[r - 1][4] = f'=IF({month}="","",SUMPRODUCT((LEFT({date},7)={month})*{paid}))'
        rows[r - 1][5] = f'=IF({month}="","",SUMPRODUCT(--(LEFT({date},7)={month})))'
    return rows


# ------------------------------------------------------------ Google
def _session():
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise SheetsError("the Google Sheets push needs google-auth: "
                          "pip install google-auth") from exc
    path = config.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    try:
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    except (OSError, ValueError) as exc:
        raise SheetsError(f"can't read the service-account key {path}: {exc}") from exc
    return AuthorizedSession(creds)


def _call(session, method: str, url: str, **kw) -> dict:
    resp = session.request(method, url, timeout=60, **kw)
    if resp.status_code >= 400:
        try:
            msg = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = resp.text[:200]
        # a 403 is also what a project without the Sheets API turned on gets
        api_off = "has not been used" in msg or "is disabled" in msg
        hint = (" (share the sheet with the service account's email as an editor)"
                if resp.status_code in (403, 404) and not api_off else "")
        raise SheetsError(f"Google Sheets {resp.status_code}: {msg}{hint}")
    return resp.json() if resp.content else {}


def _header_format(sheet_id: int, columns: int) -> list[dict]:
    """Bold, shaded, frozen header row; done once, when a tab is made."""
    return [
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 0, "endColumnIndex": columns},
                        "cell": {"userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": {"red": 0.91, "green": 0.93, "blue": 0.96}}},
                        "fields": "userEnteredFormat.textFormat.bold,"
                                  "userEnteredFormat.backgroundColor"}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id,
                                                  "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]


def _summary_format(sheet_id: int) -> list[dict]:
    def rng(r0, r1, c0, c1):
        return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
                "startColumnIndex": c0, "endColumnIndex": c1}

    def fmt(r, fields, **cell):
        return {"repeatCell": {"range": r, "cell": {"userEnteredFormat": cell}, "fields": fields}}
    money = {"type": "CURRENCY", "pattern": '$#,##0.00;($#,##0.00);"-"'}
    bold = "userEnteredFormat.textFormat.bold"
    return [
        fmt(rng(0, 1, 0, 6), bold + ",userEnteredFormat.textFormat.fontSize",
            textFormat={"bold": True, "fontSize": 14}),
        *[fmt(rng(r, r + 1, 0, 1), bold, textFormat={"bold": True}) for r in (3, 8, 18)],
        fmt(rng(1, 2, 3, 6), bold, textFormat={"bold": True}),
        *[fmt(rng(r, r + 1, 1, 2), "userEnteredFormat.numberFormat", numberFormat=money)
          for r in (5, 6, 12, 15, 20)],
        fmt(rng(2, 2 + MONTHS, 4, 5), "userEnteredFormat.numberFormat", numberFormat=money),
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                 "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 230}, "fields": "pixelSize"}},
        {"addChart": {"chart": {
            "spec": {"title": "Spent by month ($)", "basicChart": {
                "chartType": "COLUMN", "legendPosition": "NO_LEGEND", "headerCount": 1,
                "domains": [{"domain": {"sourceRange": {"sources": [rng(1, 2 + MONTHS, 3, 4)]}}}],
                "series": [{"series": {"sourceRange": {"sources": [rng(1, 2 + MONTHS, 4, 5)]}},
                            "targetAxis": "LEFT_AXIS"}]}},
            "position": {"overlayPosition": {"anchorCell": {"sheetId": sheet_id,
                                                            "rowIndex": 1, "columnIndex": 7}}}}}},
    ]


def push(con=None, snap: dict | None = None, session=None, now: datetime | None = None) -> dict:
    """Write every data tab (and the Summary, the first time). Returns rows per tab."""
    from . import snapshot   # it imports nearly everything; kept out of module import
    sheet = config.get("GOOGLE_SHEET_ID")
    if not sheet:
        raise SheetsError("GOOGLE_SHEET_ID isn't set")
    con = con or db.connect()
    snap = snap if snap is not None else snapshot.snapshot(con)
    now = now or datetime.now(config.TZ)
    data = tables(con, snap)
    data[SOURCES] = sources_rows(data, now.strftime("%Y-%m-%d %H:%M %Z"))
    session = session or _session()
    base = f"{API}/{sheet}"
    meta = _call(session, "GET", base, params={"fields": "sheets.properties"})
    have = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    rows_now = {s["properties"]["title"]: s["properties"].get("gridProperties", {}).get(
        "rowCount", 1000) for s in meta.get("sheets", [])}
    wanted = [SUMMARY, *TABS, SOURCES]
    new = [t for t in wanted if t not in have]
    if new:
        reply = _call(session, "POST", f"{base}:batchUpdate", json={"requests": [
            {"addSheet": {"properties": {"title": t}}} for t in new]})
        for r in reply.get("replies", []):
            p = r["addSheet"]["properties"]
            have[p["title"]] = p["sheetId"]
        fmt = [req for t in new if t != SUMMARY
               for req in _header_format(have[t], len(data[t][0]))]
        if SUMMARY in new:
            fmt += _summary_format(have[SUMMARY])
        # a brand-new sheet's "Sheet1" goes, but only while it is empty
        blank = "Sheet1" in have and not _call(
            session, "GET", f"{base}/values/Sheet1!A1:Z20").get("values")
        if blank:
            fmt.append({"deleteSheet": {"sheetId": have["Sheet1"]}})
        # write the Summary's formulas before the chart that reads them
        if SUMMARY in new:
            _call(session, "PUT", f"{base}/values/{SUMMARY}!A1",
                  params={"valueInputOption": "USER_ENTERED"},
                  json={"values": summary_rows()})
        _call(session, "POST", f"{base}:batchUpdate", json={"requests": fmt})
    tabs = [*TABS, SOURCES]
    # a tab is made with 1,000 rows; the sold-price ledger outgrows that
    grow = [{"appendDimension": {"sheetId": have[t], "dimension": "ROWS",
                                 "length": len(data[t]) + GROW_SPARE - rows_now.get(t, 1000)}}
            for t in tabs if len(data[t]) > rows_now.get(t, 1000)]
    if grow:
        _call(session, "POST", f"{base}:batchUpdate", json={"requests": grow})
    _call(session, "POST", f"{base}/values:batchClear",
          json={"ranges": [f"'{t}'!A:Z" for t in tabs]})
    _call(session, "POST", f"{base}/values:batchUpdate", json={
        "valueInputOption": "RAW",
        "data": [{"range": f"'{t}'!A1", "values": data[t]} for t in tabs]})
    db.kv_set(con, "sheets_pushed", json.dumps({"at": now.isoformat(timespec="minutes"),
                                                "rows": {t: len(data[t]) - 1 for t in TABS}}))
    con.commit()
    return {t: len(data[t]) - 1 for t in TABS}


def main() -> None:
    try:
        counts = push()
    except (SheetsError, config.ConfigError) as exc:
        sys.exit(f"sheets: {exc}")
    print("sheets: pushed " + ", ".join(f"{t} {n}" for t, n in counts.items()))


if __name__ == "__main__":
    main()
