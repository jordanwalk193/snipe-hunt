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
