"""Aftermarket autographs (signed in person or through the mail) are never priced
or alerted as certified autos; an auto with no proof, or a print run only the
scorer claims, can't be a jackpot."""
import pytest

from snipehunt import filters

# The listing that started this: a base insert with an IP/TTM signature, alerted
# as a jackpot priced from certified autograph sales.
REPORTED_TITLE = "2023 Bowman Scouts Top 100 Chrome Auto #BTP-5 /99 Jordan Walker"
SELLER_TITLE = "Jordan Walker Topps 2023 Bowman Scouts Top 100 Bowman Chrome Refractor Autograph"
REPORTED_DESC = ("This is an aftermarket, unauthenticated signed piece with an autograph "
                 "obtained via reputable in-person (IP) or through-the-mail (TTM) methods.")


# ------------------------------------------------------------------ words (filters)
def test_reported_description_is_aftermarket():
    assert filters.aftermarket_auto(REPORTED_TITLE, REPORTED_DESC) == "aftermarket"


@pytest.mark.parametrize("text", ["IP auto", "ttm signed", "Signed in person", "NO COA",
                                  "through the mail", "not certified", "unauthenticated"])
def test_each_strong_word_flags(text):
    assert filters.aftermarket_auto(f"Jordan Walker {text}")


def test_ip_ttm_whole_words_only():
    for t in ["Jordan Walker auto IPA", "FREE SHIPPING auto", "TTMs lot", "IP-JW auto",
              "Walker TTMRC auto"]:
        assert filters.aftermarket_auto(t) is None, t


def test_weak_words_need_no_proof():
    assert filters.aftermarket_auto("Walker hand signed card") == "hand signed"
    assert filters.aftermarket_auto("Walker hand signed on-card auto") is None
    assert filters.aftermarket_auto("Walker sharpie auto", "Certified Autograph Issue") is None


def test_negated_boilerplate_is_not_aftermarket():
    desc = "Pack pulled. We never sell aftermarket or IP autos."
    assert filters.aftermarket_auto("Walker Chrome Auto", desc) is None
    assert filters.aftermarket_auto("Walker Chrome Auto", "We do not offer TTM signatures.") is None


def test_proof_words():
    assert filters.auto_proof("x", "Topps Certified Autograph Issue")
    assert filters.auto_proof("x", "on-card autograph")
    assert filters.auto_proof("2023 Bowman Chrome CPA-JW Auto")
    assert filters.auto_proof("2023 Bowman Scouts BTPA-JW Auto")
    assert filters.auto_proof("x", "", {"Autograph Authentication": "Topps"})
    assert not filters.auto_proof("x", "", {"Autograph Authentication": "JSA"})
    assert not filters.auto_proof("x", "not certified, signed in person")
    assert not filters.auto_proof(SELLER_TITLE, REPORTED_DESC)


def test_says_auto_reads_title_or_card():
    assert filters.says_auto({"title": "Walker refractor", "card": "Scouts Auto /99"})
    assert not filters.says_auto({"title": "Walker refractor", "card": "Scouts refractor"})


def test_doubted_names_the_flag():
    assert filters.doubted({"ip_auto": True}) == "ip_auto"
    assert filters.doubted({"auto_unverified": True}) == "auto_unverified"
    assert filters.doubted({"serial_unconfirmed": True}) == "serial_unconfirmed"
    assert filters.doubted({"tier": "fire"}) is None


# ------------------------------------------------------------- reading descriptions
from snipehunt import autocheck, db, ebay_search, item_cache  # noqa: E402
from snipehunt.ebay_search import Listing  # noqa: E402


def listing(title, item_id="900000000001", price=0.99, shipping=1.99, auction=True,
            description="", aspects=None):
    return Listing(item_id=item_id, title=title, price=price, currency="USD", shipping=shipping,
                   buying_options=["AUCTION"] if auction else ["FIXED_PRICE"], bid_count=0,
                   end_time="2026-11-20T21:00-05:00", seller="made_up_seller",
                   url=f"https://ebay.com/itm/{item_id}", image=None,
                   aspects=aspects or {}, short_description=description)


class Client:
    def __init__(self, description=REPORTED_DESC):
        self.calls, self.description = [], description

    def item(self, item_id, lane="item", fresh=False):
        self.calls.append(item_id)
        return {"description": f"<p>{self.description}</p>"}


def test_enrich_reads_auto_listings_once():
    con, client = db.connect(), Client()
    first = listing(SELLER_TITLE)
    assert autocheck.enrich(con, [first], client=client) == 1
    again = listing(SELLER_TITLE)
    assert autocheck.enrich(con, [again], client=client) == 1
    assert client.calls == ["900000000001"]           # the second run read the table
    for item in (first, again):
        assert "aftermarket_auto" in item.flags and item.auto_proof is False
        assert "in-person" in item.description


def test_enrich_skips_non_auto_titles():
    client = Client()
    assert autocheck.enrich(db.connect(), [listing("2023 Bowman Scouts Top 100 Walker")],
                            client=client) == 0
    assert client.calls == []


def test_enrich_reads_a_signed_listing_from_its_specifics():
    client = Client("Topps Certified Autograph Issue")
    item = listing("2023 Bowman Scouts Top 100 Walker", aspects={"Autographed": "Yes"})
    assert autocheck.enrich(db.connect(), [item], client=client) == 1
    assert item.auto_proof is True and "aftermarket_auto" not in item.flags


def test_enrich_stops_at_limit():
    client = Client()
    items = [listing(SELLER_TITLE, item_id=f"90000000000{n}") for n in range(3)]
    assert autocheck.enrich(db.connect(), items, client=client, limit=2) == 2
    assert len(client.calls) == 2
    assert "aftermarket_auto" not in items[2].flags and items[2].auto_proof is False


def test_description_text_strips_html():
    raw = {"description": "<b>Topps&nbsp;Certified</b>\n\n Autograph<script>x()</script>"}
    assert ebay_search.description_text(raw) == "Topps Certified Autograph"
    assert len(ebay_search.description_text({"description": "a " * 5000})) <= ebay_search.DESCRIPTION_CHARS


def test_cache_keeps_description_text():
    con = db.connect()
    item_cache.put(con, "900000000009", {"description": "<p>signed in person</p>", "title": "t"})
    assert item_cache.get(con, "900000000009")["description"] == "signed in person"


# ------------------------------------------------------------- the pick and its caps
from datetime import datetime, timedelta  # noqa: E402

from snipehunt import config, jackpot, score  # noqa: E402

SOON = (datetime.now(config.TZ) + timedelta(minutes=45)).isoformat(timespec="minutes")
# What the scorer said about the reported listing
CLAIM = {"card": REPORTED_TITLE, "tier": "fire", "edge": "hidden", "value": 43, "max": 18.18,
         "max_basis": "sales", "why": "Chrome auto at $0.99 with no bids."}


def scored(description="", aspects=None, claim=None):
    item = listing(SELLER_TITLE, aspects=aspects)
    item.end_time = SOON
    item.description = description
    item.auto_proof = filters.auto_proof(item.title, description, item.aspects)
    filters.flag(item)
    return score._pick_dict(item, dict(claim or CLAIM))


def test_reported_listing_is_an_ip_auto_not_a_jackpot():
    pick = scored(REPORTED_DESC)
    assert pick["ip_auto"] and pick["aftermarket"] == "aftermarket"
    assert pick["tier"] == "yellow" and pick["edge"] is None
    assert pick["value"] <= score.IP_VALUE_CAP and pick["max"] <= pick["value"]
    assert pick["deal"] <= score.IP_STARS
    assert pick["why"].startswith(filters.AFTERMARKET_WHY)
    assert jackpot.check(pick) is None
    again = score.apply_caps(dict(pick))
    assert again == pick                                  # the caps hold on every re-rate


def test_auto_without_proof_far_under_value_is_unverified():
    pick = scored("Great card, ships fast.")
    assert "ip_auto" not in pick and pick["auto_proof"] is False
    assert pick["auto_unverified"] and pick["tier"] == "yellow" and pick["edge"] is None


def test_auto_with_proof_keeps_its_tier():
    pick = scored("Topps Certified Autograph Issue", aspects={"Print Run": "99"})
    assert pick["auto_proof"] is True and pick["tier"] == "fire" and pick["edge"] == "hidden"
    assert "auto_unverified" not in pick and "serial_unconfirmed" not in pick


def test_serial_only_in_card_name_is_unconfirmed():
    pick = scored("Topps Certified Autograph Issue")
    assert pick["serial_unconfirmed"] and pick["tier"] == "green" and pick["edge"] is None


def test_serial_in_specifics_is_confirmed():
    assert filters.serial_unconfirmed(REPORTED_TITLE, SELLER_TITLE, {"Print Run": "99"}, "") is None
    assert filters.serial_unconfirmed(REPORTED_TITLE, SELLER_TITLE, {}, "numbered 12/99") is None
    assert filters.serial_unconfirmed(REPORTED_TITLE, SELLER_TITLE, {}, "") == "99"
    assert filters.serial_unconfirmed("Scouts Top 100 refractor", SELLER_TITLE, {}, "") is None


def test_ip_pool_comps_are_kept():
    pick = score.apply_caps({"ip_auto": True, "comps_n": 3, "value": 14.0, "max": 6.06,
                             "price": 1.0, "shipping": 1.0, "is_auction": True, "tier": "green",
                             "why": "", "max_basis": "sales"})
    assert pick["value"] == 14.0 and pick["tier"] == "yellow"


def test_prompt_warns_on_aftermarket():
    item = listing(SELLER_TITLE, description=REPORTED_DESC)
    filters.flag(item)
    assert filters.AFTERMARKET_WHY in score._compact(item)
    assert score.AFTERMARKET_RULE in score.build_prompt([item])


# ------------------------------------------------- jackpots, bid-now, steals, alerts
from snipehunt import bidnow, notify  # noqa: E402


def cheap_auto(**kw):
    return {"item_id": "900000000050", "card": "2023 Chrome Rookie Auto /99", "tier": "green",
            "price": 0.99, "shipping": 1.00, "total": 1.99, "value": 30.0, "max_basis": "sales",
            "is_auction": True, "end_time": SOON, "max": 15.15, "auto_proof": True,
            "url": "https://www.ebay.com/itm/900000000050", **kw}


@pytest.mark.parametrize("flag", filters.DOUBTS)
def test_doubted_picks_never_jackpot_bidnow_or_alert(flag, monkeypatch):
    monkeypatch.setattr(notify, "is_quiet", lambda now=None: False)
    p = cheap_auto(**{flag: True})
    assert jackpot.check(p) is None
    assert not bidnow._trusted(p)
    assert not notify.worth_alerting(p)


def test_unread_auto_cannot_be_jackpot():
    p = cheap_auto()
    del p["auto_proof"]
    assert jackpot.check(p) is None


def test_certified_auto_still_jackpots():
    assert jackpot.check(cheap_auto()) == "sales"


def test_ledger_verify_drops_aftermarket():
    class Ebay:
        def item(self, item_id, **kw):
            return {"itemId": f"v1|{item_id}|0", "legacyItemId": item_id, "title": SELLER_TITLE,
                    "buyingOptions": ["FIXED_PRICE"], "price": {"value": "3.50", "currency": "USD"},
                    "shippingOptions": [{"shippingCost": {"value": "1.00", "currency": "USD"}}],
                    "seller": {"username": "made_up_seller", "feedbackScore": 250},
                    "description": f"<div>{REPORTED_DESC}</div>"}
    found = [{"item_id": "900000000060", "title": SELLER_TITLE, "card": SELLER_TITLE,
              "is_auction": False, "url": "u", "value": 40.0, "max_basis": "sales"}]
    assert jackpot.verify(found, Ebay()) == []
    proven = dict(found[0], item_id="900000000061")

    class Proven(Ebay):
        def item(self, item_id, **kw):
            return dict(super().item(item_id), description="Topps Certified Autograph Issue")
    kept = jackpot.verify([proven], Proven())
    assert kept and kept[0]["auto_proof"] is True


def test_ledger_scan_skips_aftermarket_titles():
    assert filters.aftermarket_auto("2023 Bowman Jordan Walker TTM Auto")   # what scan() checks


# ------------------------------------------------------------- IP sales: their own pool
from snipehunt import ledger, sales  # noqa: E402
from tests.test_ledger import sale  # noqa: E402

IP_TITLE = "2023 Bowman Scouts Top 100 Jordan Walker Auto TTM"
CERT_TITLE = "2023 Bowman Scouts Top 100 Jordan Walker Auto"


def test_ip_sale_is_not_same_card_as_certified():
    assert sales.traits(IP_TITLE)[1] == "ip" and sales.traits(CERT_TITLE)[1] is True
    assert not sales._same(IP_TITLE, CERT_TITLE)


def test_item_traits_reads_the_flag():
    item = listing(SELLER_TITLE)
    item.flags.append("aftermarket_auto")
    assert sales.item_traits(item)[1] == "ip"


def test_ip_sales_group_together():
    con = db.connect()
    sale(con, "900000000071", IP_TITLE, 6.0, 3)
    sale(con, "900000000072", "2023 Bowman Scouts Top 100 Jordan Walker Auto IP", 7.0, 5)
    sale(con, "900000000073", CERT_TITLE, 45.0, 4)
    con.commit()
    gs = ledger.groups(con)
    ip = [g for g in gs if g["traits"][1] == "ip"]
    assert len(ip) == 1 and len(ip[0]["sales"]) == 2
    assert any(g["traits"][1] is True and len(g["sales"]) == 1 for g in gs)


def test_ip_sale_never_values_an_inventory_row():
    row = {"year": "2023", "set": "Bowman", "card": "Scouts Top 100", "auto": "Y"}
    g = {"title": IP_TITLE, "traits": sales.traits(IP_TITLE), "sales": [{}]}
    assert ledger._row_fit(ledger.row_parts(row), g) is None


# ------------------------------------------------------------- the dashboard's list
from snipehunt import settings, snapshot  # noqa: E402
from tests.test_snapshot import NOW as SNAP_NOW, add  # noqa: E402


def test_ip_autos_hidden_by_default():
    con = db.connect()
    add(con, "900000000081", ip_auto=True, tier="yellow", aftermarket="ttm")
    add(con, "900000000082")
    con.commit()
    snap = snapshot.snapshot(con, SNAP_NOW)
    assert [p["item_id"] for p in snap["picks"]] == ["900000000082"]
    assert [p["item_id"] for p in snap["ip_autos"]] == ["900000000081"]
    assert snap["ip_autos"][0]["ip_auto"] and snap["ip_autos"][0]["aftermarket"] == "ttm"
    settings.update(con, {"show_ip_autos": True})
    shown = snapshot.snapshot(con, SNAP_NOW)
    assert {p["item_id"] for p in shown["picks"]} == {"900000000081", "900000000082"}


def test_snapshot_carries_the_doubt_flags():
    con = db.connect()
    add(con, "900000000083", auto_unverified=True)
    con.commit()
    assert snapshot.snapshot(con, SNAP_NOW)["picks"][0]["auto_unverified"] is True
