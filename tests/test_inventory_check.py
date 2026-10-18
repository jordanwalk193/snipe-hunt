"""The inventory check lists problems and never touches the CSV."""
import hashlib
import subprocess
import sys
from datetime import date

from snipehunt import config, inventory, known_cards

HEADER = ("id,year,set,card,parallel,card_no,serial,rc,auto,relic,graded,front_page,dupe,"
          "status,price_paid,order_date,seller,notes,ebay_item_id\n")
ROWS = [
    "1,2021,Bowman Chrome,Prospect,Atomic,FMF-10,045/150,,,,,Y,,In binder,22.22,2026-09-01,shop-a,,900000000001",
    "2,2021,Bowman Chrome,Prospect,Atomic,FMF-10,045/150,,,,,,,In binder,22.22,2026-09-01,shop-a,,900000000002",
    "3,2023,Bowman Heritage,Base,,BHP-52,,,,,,,,Delivered,1.50,2026-09-20,shop-b,,900000000003",
    "4,2022,Bowman Chrome,Prospect,,BCP-236,,,,,,,,In transit,3.03,2026-10-01,shop-c,due 2026-10-10,900000000004",
    "5,2023,Topps,Base,,,,,,,,,Y,In binder,0.99,2026-08-01,shop-d,Same item as #3,",
]
TODAY = date(2026, 10, 18)


def write():
    (config.DATA_DIR / "inventory.csv").write_text(HEADER + "\n".join(ROWS) + "\n")
    (config.DATA_DIR / known_cards.FILE).write_text(
        (config.EXAMPLES / "known_cards.example.toml").read_text())


def found():
    write()
    _, rows = inventory.read()
    return inventory.check(rows, known_cards.load(with_inventory=False), TODAY)


def test_duplicates_and_same_item_notes():
    got = found()["duplicates"]
    assert any("#1 " in d and "#2 " in d for d in got)
    assert any("#5 " in d and "Same item as #3" in d for d in got)


def test_stale_statuses():
    stale = found()["stale"]
    assert any(s.startswith("#3 ") and "Delivered" in s for s in stale)   # 28 days, not filed
    assert any(s.startswith("#4 ") and "was due 2026-10-10" in s for s in stale)
    assert not any(s.startswith("#1 ") for s in stale)


def test_year_mismatches_against_known_cards():
    got = found()["year_mismatch"]
    assert any(s.startswith("#4 ") and "2021" in s for s in got)           # BCP-236 is 2021
    assert any(s.startswith("#3 ") and "a common mix-up" in s for s in got)  # BHP-52 drift
    assert not any(s.startswith("#1 ") for s in got)


def test_missing_numbers_trade_pile_and_front_page():
    got = found()
    assert got["missing_card_no"] == ["#5 2023 Topps Base"]
    assert got["missing_item_id"] == ["#5 2023 Topps Base"]
    assert got["trade_pile"] == ["#5 2023 Topps Base"]
    assert got["front_page"] == ["1 card marked front_page=Y; the page holds 9"]
    assert inventory.check_summary(got).startswith("Inventory check: 2 possible duplicates")


def test_the_csv_is_never_modified():
    write()
    path = config.DATA_DIR / "inventory.csv"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    found()
    env = {"SNIPEHUNT_HOME": str(config.HOME), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(config.EXAMPLES.parent)}
    (config.DATA_DIR / "profile.toml").write_text(
        (config.EXAMPLES / "profile.example.toml").read_text())
    run = subprocess.run([sys.executable, "-m", "snipehunt", "inventory", "check"],
                         capture_output=True, text=True, env=env, cwd=str(config.HOME))
    assert run.returncode == 0, run.stderr
    assert "Possible duplicates" in run.stdout and "Year mismatches" in run.stdout
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_the_weekly_report_has_the_check():
    from snipehunt import db, report
    write()
    text, summary = report.build(db.connect())
    assert "## Inventory check" in text and "Trade pile (dupe=Y)" in text
    assert summary.splitlines()[-1].startswith("Inventory check:")
