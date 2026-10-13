from snipehunt.ebay_search import classify, parse_item

YES = [
    "2023 Topps Chrome Jordan Walker RC #209 Cardinals",
    "JORDON WALKER 2020 Bowman Draft 1st BD-57",
    "2019 Leaf Perfect Game Jordan Walker Auto",
    "J. Walker Cardinals 2023 Topps Heritage RC",
    "2023 Topps Jordan Wlaker rookie St Louis",
]
NO = [
    "2022 Bowman University Chrome Jordan Walker UAB Blazers",
    "Jordan Walker Dallas Mavericks Panini Prizm RC",
    "Larry Walker Cardinals 2005 Topps",
    "Michael Jordan / Larry Walker dual relic",
    "Taijuan Walker 2023 Topps Phillies",
]
UNCLEAR = [
    "Jordan Walker RC lot of 5",
    "Walker Cardinals rookie lot (10)",
]


def test_classify():
    for t in YES:
        assert classify(t) == "yes", t
    for t in NO:
        assert classify(t) == "no", t
    for t in UNCLEAR:
        assert classify(t) == "unclear", t


def test_parse_auction():
    item = parse_item({
        "itemId": "v1|123456789012|0", "legacyItemId": "123456789012",
        "title": "2023 Topps Jordan Walker RC", "buyingOptions": ["AUCTION"],
        "price": {"value": "20.00", "currency": "USD"},
        "currentBidPrice": {"value": "0.99", "currency": "USD"},
        "shippingOptions": [{"shippingCost": {"value": "1.25", "currency": "USD"}}],
        "bidCount": 1, "itemEndDate": "2026-10-12T01:30:00.000Z",
        "seller": {"username": "bigseller"}, "image": {"imageUrl": "http://x/y.jpg"},
    })
    assert item.price == 0.99 and item.total == 2.24
    assert item.end_time.startswith("2026-10-11T21:30")
    assert item.url.endswith("/123456789012")
