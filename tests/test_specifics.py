from snipehunt import db, lowball, specifics
from snipehunt.ebay_search import Listing


def listing(title, price=3.0, shipping=1.0, item_id="1", auction=True, match="yes"):
    return Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=shipping,
                   buying_options=["AUCTION"] if auction else ["FIXED_PRICE"], bid_count=0,
                   end_time="2026-11-20T21:00-05:00", seller="s", url=f"https://ebay.com/itm/{item_id}",
                   image=None, match=match)


def raw(**specs):
    return {"localizedAspects": [{"name": k, "value": v} for k, v in specs.items()]}


def test_vague_titles_lack_a_year_or_a_set():
    assert specifics.vague("Jordan Walker PRIZM Rookie Draft Picks Power Surge RC")
    assert specifics.vague("Topps Chrome X-fractor Parallel Jordan Walker #221")
    assert not specifics.vague("2023 Topps Chrome Jordan Walker #209 Refractor")
    assert not specifics.vague("Jordan Walker Lot of 4, 2 RC, 1 Refractor")
    assert not specifics.vague("(10) Jordan Walker - RC and parallels topps, bowman")
    assert not specifics.vague("Huge Cardinals 900+ Card Lot Walker Rookies")


def test_identity_reads_the_specifics_and_skips_empty_values():
    assert specifics.identity(raw(**{"Year Manufactured": "2020", "Manufacturer": "Panini",
                                    "Set": "2020 Panini Prizm Draft Picks", "Card Number": "PDP169",
                                    "Features": "Rookie", "Graded": "No"})) \
        == "2020 Panini Prizm Draft Picks #PDP169, Rookie"
    assert specifics.identity(raw(**{"Manufacturer": "Topps", "Set": "Topps Chrome", "Card Number": "221",
                                    "Features": "Parallel/Variety", "Parallel/Variety": "X-Fractor"})) \
        == "Topps Chrome #221, X-Fractor"
    assert specifics.identity(raw(**{"Season": "2023", "Set": "Does Not Apply", "Card Number": "N/A"})) is None
    assert specifics.identity(raw(**{"Set": "2024 Topps Chrome", "Card Number": "#36", "Autographed": "Yes",
                                    "Professional Grader": "PSA", "Grade": "10"})) \
        == "2024 Topps Chrome #36, Autograph, PSA 10"
    assert specifics.identity(raw(**{"Year Manufactured": "2020", "Set": "Bowman Draft 1st Edition",
                                    "Card Number": "BD-57", "Parallel/Variety": "[Base]",
                                    "Features": "Base Set, Rookie"})) \
        == "2020 Bowman Draft 1st Edition #BD-57, Rookie"
    assert specifics.identity(None) is None


def test_enrich_looks_each_item_up_once_and_marks_the_listing():
    con = db.connect()
    calls = []

    class Client:
        def item(self, item_id, lane="item", fresh=False):
            calls.append(item_id)
            return raw(**{"Year Manufactured": "2020", "Set": "Panini Prizm Draft Picks",
                          "Card Number": "PDP169", "Features": "Rookie"}) if item_id == "1" else {}
    vague = listing("Jordan Walker PRIZM Rookie Draft Picks Power Surge RC", item_id="1")
    clear = listing("2023 Topps Chrome Jordan Walker #209", item_id="2")
    blank = listing("Jordan Walker Rookie Refractor", item_id="3")
    other = listing("Larry Walker Rookie Refractor", item_id="4", match="no")
    assert specifics.enrich(con, [vague, clear, blank, other], client=Client()) == 1
    assert calls == ["1", "3"]   # the clear title and the other player cost nothing
    assert vague.title.endswith("[2020 Panini Prizm Draft Picks #PDP169, Rookie]")
    assert "specifics" in vague.queries and clear.title == "2023 Topps Chrome Jordan Walker #209"
    # the next run reads the table, not eBay, and doesn't double the brackets
    again = listing("Jordan Walker PRIZM Rookie Draft Picks Power Surge RC", item_id="1")
    assert specifics.enrich(con, [again, listing("Jordan Walker Rookie Refractor", item_id="3")], client=Client()) == 1
    assert calls == ["1", "3"] and again.title.count("[") == 1
    assert specifics.enrich(con, [again], client=Client()) == 0


def test_enrich_respects_the_per_run_budget():
    con = db.connect()
    looked = []

    class Client:
        def item(self, item_id, lane="item", fresh=False):
            looked.append(item_id)
            return raw(**{"Set": "2023 Topps", "Card Number": str(item_id)})
    items = [listing("Jordan Walker Rookie", item_id=str(n)) for n in range(5)]
    assert specifics.enrich(con, items, client=Client(), limit=2) == 2
    assert looked == ["0", "1"]


def test_lowball_level_follows_the_ending_hour():
    cheap_night = [{"share": s / 20, "hour": 3} for s in range(1, 21)]          # 0.05 .. 1.00
    dear_evening = [{"share": 0.8 + s / 50, "hour": 21} for s in range(1, 21)]  # 0.82 .. 1.20
    results = cheap_night + dear_evening
    overall = lowball.level(results)
    assert lowball.level_for(results, 4) < overall < lowball.level_for(results, 22)
    assert lowball.level_for(results, 22) == lowball.LEVEL_RANGE[1]
    assert lowball.level_for(results, 12) == overall     # no record for noon: the overall level
    assert lowball.level_for(cheap_night[:5], 4) == lowball.level(cheap_night[:5])
