import json
from datetime import datetime

import pytest

from snipehunt import config, db, jobs, sheets

NOW = datetime(2026, 10, 24, 19, 0, tzinfo=config.TZ)


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self._body = status, body
        self.content = b"x" if body is not None else b""
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """Answers like the Sheets API and keeps every call."""

    def __init__(self, tabs=("Sheet1",), status=200, rows=1000):
        self.tabs, self.status, self.calls, self.next_id = list(tabs), status, [], 100
        self.rows = rows

    def request(self, method, url, timeout=None, params=None, json=None):
        self.calls.append((method, url.split("/spreadsheets/")[1], params, json))
        if self.status != 200:
            return Resp({"error": {"message": "The caller does not have permission"}}, self.status)
        if method == "GET" and url.endswith("sheet-id"):
            return Resp({"sheets": [{"properties": {"title": t, "sheetId": i, "gridProperties":
                                                    {"rowCount": self.rows}}}
                                    for i, t in enumerate(self.tabs)]})
        if method == "GET":   # Sheet1's values: empty
            return Resp({})
        if url.endswith(":batchUpdate") and json and json.get("requests", [{}])[0].get("addSheet"):
            replies = []
            for r in json["requests"]:
                self.next_id += 1
                replies.append({"addSheet": {"properties": {
                    "title": r["addSheet"]["properties"]["title"], "sheetId": self.next_id}}})
            self.tabs += [r["addSheet"]["properties"]["title"] for r in replies]
            return Resp({"replies": replies})
        return Resp({})


SNAP = {
    "picks": [{"item_id": "1001", "card": "2023 Chrome Rookie Gold /50", "tier": "green",
               "deal": 4, "price": 12.5, "shipping": 1.0, "max": 18.18, "value": 30.0,
               "max_basis": "sales", "deal_pct": 36, "is_auction": True,
               "end_time": "2026-10-25T21:30-04:00", "seller": "shop_a", "snipe": None}],
    "offers": [{"item_id": "1002", "title": "=HYPERLINK(\"http://evil\")", "price": 20.0,
                "shipping": 1.0, "days": 45, "offer": 13.13, "value": None, "basis": None,
                "seller": "shop_b"}],
    "lowball": {"targets": [{"item_id": "1003", "title": "Rookie Auto /199", "price": 2.0,
                             "shipping": None, "bids": 0, "end_time": "2026-10-25T03:00-04:00",
                             "value": 40.0, "snipe": 9.09, "dead": "ends overnight",
                             "overlooked": ["shipping not stated"], "sniped": True}]},
    "gixen": {"snipes": [{"item_id": "1004", "title": "Prospect Refractor", "max_bid": 7.07,
                          "current_bid": 3.0, "end_time": "2026-10-26T20:00-04:00",
                          "check": {"label": "A real discount if it holds", "value": 15.0},
                          "doubts": [{"kind": "twice", "text": "Also sniped as item 1005"}]}]},
}


@pytest.fixture
def sheet_env(monkeypatch):
    monkeypatch.setitem(config._cache, "GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setitem(config._cache, "GOOGLE_SERVICE_ACCOUNT_FILE", "/nowhere/key.json")


def test_rows_keep_titles_as_text_and_money_as_numbers():
    con = db.connect()
    data = sheets.tables(con, SNAP)
    assert set(data) == set(sheets.TABS)
    for tab, rows in data.items():
        assert rows[0] == sheets.HEADERS[tab]
        assert all(len(r) == len(rows[0]) for r in rows)
    pick = data["Picks"][1]
    assert pick[:4] == ["green", 4, "2023 Chrome Rookie Gold /50", "Auction"]
    assert pick[10] == "2026-10-25 21:30" and pick[-1] == "https://www.ebay.com/itm/1001"
    assert data["Lowball"][1][7:10] == ["ends overnight", "shipping not stated", "yes"]
    assert data["Snipes"][1][6] == "Also sniped as item 1005"
    # the example inventory: paid is a number, a missing price an empty cell
    paid = [r[12] for r in data["Collection"][1:]]
    assert any(isinstance(p, float) for p in paid) and all(p is None or isinstance(p, float)
                                                           for p in paid)


def test_first_push_builds_the_sheet_and_writes_data_raw(sheet_env):
    con = db.connect()
    s = FakeSession()
    counts = sheets.push(con, SNAP, session=s, now=NOW)
    assert counts["Picks"] == 1 and counts["Offers"] == 1
    added = [r["addSheet"]["properties"]["title"] for r in s.calls[1][3]["requests"]]
    assert added == ["Summary", *sheets.TABS, "Data Sources"]
    summary = next(c for c in s.calls if c[0] == "PUT")
    assert summary[2] == {"valueInputOption": "USER_ENTERED"}
    assert summary[3]["values"][4][1].startswith("=COUNTIFS(Collection!")
    fmt = next(c for c in s.calls if c[1].endswith(":batchUpdate")
               and any("deleteSheet" in r for r in c[3]["requests"]))
    assert any("addChart" in r for r in fmt[3]["requests"])     # the empty Sheet1 goes too
    write = s.calls[-1]
    assert write[3]["valueInputOption"] == "RAW"                # titles never run as formulas
    offers = next(d for d in write[3]["data"] if d["range"] == "'Offers'!A1")
    assert offers["values"][1][0] == '=HYPERLINK("http://evil")'
    assert json.loads(db.kv_get(con, "sheets_pushed"))["rows"]["Lowball"] == 1


def test_later_pushes_leave_the_summary_alone(sheet_env):
    con = db.connect()
    s = FakeSession(tabs=["Summary", *sheets.TABS, "Data Sources", "My notes"])
    sheets.push(con, SNAP, session=s, now=NOW)
    methods = [(m, u) for m, u, _, _ in s.calls]
    assert methods == [("GET", "sheet-id"), ("POST", "sheet-id/values:batchClear"),
                       ("POST", "sheet-id/values:batchUpdate")]
    cleared = s.calls[1][3]["ranges"]
    assert "'Summary'!A:Z" not in cleared and "'My notes'!A:Z" not in cleared


def test_a_sheet_not_shared_says_how_to_fix_it(sheet_env):
    with pytest.raises(sheets.SheetsError, match="share the sheet"):
        sheets.push(db.connect(), SNAP, session=FakeSession(status=403), now=NOW)


def test_off_until_both_settings_are_there(monkeypatch):
    assert not sheets.configured()
    monkeypatch.setitem(config._cache, "GOOGLE_SHEET_ID", "x")
    assert not sheets.configured()


def test_a_failed_push_never_stops_the_hourly_run(sheet_env, monkeypatch):
    monkeypatch.setattr(sheets, "push", lambda con: (_ for _ in ()).throw(
        sheets.SheetsError("Google Sheets 500")))
    monkeypatch.setattr(jobs.ebay_search, "search_all", lambda verbose=False: [])
    monkeypatch.setitem(config._cache, "EBAY_CLIENT_ID", "id")
    monkeypatch.setitem(config._cache, "EBAY_CLIENT_SECRET", "secret")
    con = db.connect()
    db.touch(con, "1", "t")   # not the first run
    con.commit()
    assert "0 listings" in jobs.hourly()


def test_the_month_table_rows_line_up():
    rows = sheets.summary_rows()
    assert rows[2][3].startswith("=IFERROR(SORT(UNIQUE(")
    last = 2 + sheets.MONTHS
    assert rows[2][4].startswith('=IF(D3=""') and rows[-1][4].startswith(f'=IF(D{last}=""')


def test_a_tab_too_small_for_the_data_grows_first(sheet_env):
    s = FakeSession(tabs=["Summary", *sheets.TABS, "Data Sources"], rows=2)
    sheets.push(db.connect(), SNAP, session=s, now=NOW)
    grow = next(c for c in s.calls if c[1].endswith(":batchUpdate") and "requests" in c[3])
    lengths = {r["appendDimension"]["sheetId"]: r["appendDimension"]["length"]
               for r in grow[3]["requests"]}
    collection = s.tabs.index("Collection")
    assert lengths[collection] > 0 and s.tabs.index("Lowball") not in lengths  # 2 rows fit
    assert grow is s.calls[1]   # before anything is written


def test_an_api_that_is_off_is_not_blamed_on_sharing(sheet_env):
    class Off(FakeSession):
        def request(self, method, url, timeout=None, params=None, json=None):
            return Resp({"error": {"message": "Google Sheets API has not been used in "
                                   "project 1 before or it is disabled."}}, 403)
    with pytest.raises(sheets.SheetsError) as err:
        sheets.push(db.connect(), SNAP, session=Off(), now=NOW)
    assert "has not been used" in str(err.value) and "share the sheet" not in str(err.value)
