from snipehunt import config, inventory, musthave, profile, snapshot

ROWS = [
    {"id": "6", "year": "2020", "set": "Bowman Draft Chrome", "card": "1st Bowman prospect",
     "parallel": "Asia Mojo Refractor", "card_no": "BD-57", "graded": "", "auto": "",
     "status": "Delivered"},
    {"id": "112", "year": "2020", "set": "Bowman Draft Chrome", "card": "1st Bowman prospect",
     "parallel": "Refractor", "card_no": "BD-57", "graded": "", "auto": "", "status": "In transit"},
    {"id": "5", "year": "2020", "set": "Bowman Draft", "card": "1st Bowman prospect (paper)",
     "parallel": "Base", "card_no": "BD-57", "graded": "", "auto": "", "status": "In binder"},
]


def card(owned_when, year="2020"):
    return {"year": year, "owned_when": owned_when}


def use_example():
    (config.DATA_DIR / "musthave.md").write_text(
        (config.EXAMPLES / "musthave.example.md").read_text())


def test_no_file_means_an_empty_list(monkeypatch):
    monkeypatch.setattr(musthave, "FILE", config.DATA_DIR / "musthave.md")
    assert musthave.read() == []
    assert musthave.view(ROWS, [], []) == {"years": [], "owned": 0, "total": 0}


def test_the_example_reads_and_parses_against_the_example_inventory(monkeypatch):
    monkeypatch.setattr(musthave, "FILE", config.DATA_DIR / "musthave.md")
    use_example()
    cards = musthave.read()
    assert len(cards) == 5 and {c["year"] for c in cards} == {"2020", "2023"}
    view = musthave.view(inventory.read()[1], [], [])
    owned = [c["card"] for y in view["years"] for c in y["cards"] if c["owned"]]
    assert owned == ["Bowman Draft Chrome 1st Bowman BD-57",   # example row 2, a PSA 9
                     "Bowman Draft Chrome BD-57, graded",      # the same row
                     "Topps Chrome rookie #209 Refractor"]     # example row 1


def test_owned_when_rules():
    ids = lambda rule: [r["id"] for r in musthave.owners(card(rule), ROWS)]   # noqa: E731
    assert ids("Bowman Draft -Chrome; BD-57;") == ["5"]
    assert ids("Bowman Draft Chrome; BD-57; Refractor -Mojo") == ["112"]
    assert ids("; BD-57; Asia Mojo") == ["6"]
    assert ids("; BD-57; graded") == []
    assert ids("; BD-57;") == ["5", "6", "112"]          # best status first
    assert musthave.owners(card("; BD-57;", year="2021"), ROWS) == []


def test_search_matches_titles_like_ebay_would():
    search = "jordan walker 2020 bowman draft chrome bd-57 refractor -auto"
    assert musthave.matches(search, "2020 Bowman Draft Chrome Jordan Walker #BD57 Refractor PSA 9")
    assert musthave.matches(search, "2020 Bowman Draft Chrome Jordon Walker BD-57 Refractor")
    assert not musthave.matches(search, "2020 Bowman Draft Chrome BD-57 Refractor Auto /499")
    assert not musthave.matches("jordan walker 2023 bowman chrome 18 rc",
                                "2018 Bowman Chrome Jordan Walker RC")   # 18 is not in 2018


def test_the_name_comes_from_the_profile(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.Profile(player="Sam Example"))
    assert musthave.matches("sam example 2024 topps 7", "2024 Topps #7 Sam Exampel")
    assert not musthave.matches("jordan walker 2024 topps 7", "2024 Topps #7 Sam Example")


def test_view_marks_owned_for_sale_and_picks(monkeypatch):
    monkeypatch.setattr(musthave, "read", lambda: [{
        "year": "2020", "card": "BD-57 Refractor", "why": "", "rate": "~$10",
        "search": "jordan walker 2020 bowman draft chrome bd-57 refractor -auto",
        "owned_when": "Bowman Draft Chrome; BD-57; Refractor -Mojo"}])
    listings = [{"title": "2020 Bowman Draft Chrome Jordan Walker BD-57 Refractor", "total": 9.5},
                {"title": "2020 Bowman Draft Chrome Jordan Walker BD-57 Refractor", "total": 7.25}]
    picks = [{"item_id": "1", "title": listings[0]["title"], "tier": "green", "deal": 3,
              "total": 7.25, "url": "u"}]
    v = musthave.view(ROWS, listings, picks)
    one = v["years"][0]["cards"][0]
    assert (v["owned"], v["total"], one["owned"]["id"]) == (1, 1, "112")
    assert (one["for_sale"], one["cheapest"], one["pick"]["item_id"]) == (2, 7.25, "1")
    assert one["url"].startswith("https://www.ebay.com/sch/i.html?_nkw=jordan+walker+2020")
    assert one["url"].endswith("&_sacat=261328")


def test_the_link_follows_the_profiles_category(monkeypatch):
    monkeypatch.setattr(profile, "_cache", profile.Profile(player="Charizard", category=""))
    assert "_sacat" not in musthave.ebay_url("charizard 4/102")


def test_the_snapshot_carries_both_lists(monkeypatch):
    monkeypatch.setattr(musthave, "FILE", config.DATA_DIR / "musthave.md")
    from snipehunt import rainbow
    monkeypatch.setattr(rainbow, "FILE", config.DATA_DIR / "rainbow.md")
    snap = snapshot.snapshot()
    assert snap["must_have"]["years"] == [] and snap["rainbows"] == []
    use_example()
    assert snapshot.snapshot()["must_have"]["total"] == 5
