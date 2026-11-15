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
