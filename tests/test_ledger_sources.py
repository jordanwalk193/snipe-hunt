"""Sold prices from your own wins (Gixen and eBay emails) and from you."""
import subprocess
import sys

from snipehunt import config, db, sales


def mail(con, item_id, price, source="ebay", title="2021 Bowman Chrome Jordan Walker /150",
         date="2026-10-10", shipping=1.25):
    ev = {"type": "won", "is_player": True, "item_id": item_id, "title": title, "price": price,
          "shipping": shipping, "date": date, "source": source, "seller": "made-up"}
    db.log_mail_events(con, [ev])
    con.commit()


def test_won_emails_become_sales_once_each():
    con = db.connect()
    mail(con, "900000000001", 22.22, source="gixen")
    mail(con, "900000000002", 15.15)
    mail(con, "900000000002", 15.15, date="2026-10-11")   # the same win, mailed twice
    assert sales.from_mail(con) == 2
    assert sales.from_mail(con) == 0                       # nothing new the second time
    got = {r["item_id"]: dict(r) for r in con.execute("SELECT * FROM sales")}
    assert got["900000000001"]["source"] == "gixen" and got["900000000001"]["price"] == 22.22
    assert got["900000000002"]["source"] == "won" and got["900000000002"]["shipping"] == 1.25


def test_a_price_the_bot_looked_up_is_kept_over_the_email():
    con = db.connect()
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at)"
                " VALUES ('900000000003', 't', 30.0, 1.0, 4, 1)")
    mail(con, "900000000003", 29.0)
    assert sales.from_mail(con) == 0
    assert con.execute("SELECT source, price FROM sales").fetchone()[:] == ("ebay", 30.0)


def test_emails_without_a_price_or_item_number_are_skipped():
    con = db.connect()
    mail(con, "", 10.0)
    mail(con, "900000000004", None)
    assert sales.from_mail(con) == 0


def test_manual_sale_command(tmp_path):
    env = {"SNIPEHUNT_HOME": str(config.HOME), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(config.EXAMPLES.parent)}
    (config.DATA_DIR / "profile.toml").write_text(
        (config.EXAMPLES / "profile.example.toml").read_text())
    run = subprocess.run([sys.executable, "-m", "snipehunt", "sale", "add", "900000000005",
                          "33.33", "4.50", "2023 Bowman Chrome Jordan Walker SOS-19 /150"],
                         capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert run.returncode == 0, run.stderr
    row = db.connect().execute("SELECT * FROM sales WHERE item_id='900000000005'").fetchone()
    assert (row["price"], row["shipping"], row["source"]) == (33.33, 4.5, "manual")
    bad = subprocess.run([sys.executable, "-m", "snipehunt", "sale", "add", "x", "1", "0", "t"],
                         capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert bad.returncode != 0 and "digits" in bad.stderr


def test_the_sold_block_names_the_source():
    con = db.connect()
    sales.add_manual(con, "900000000006", 40.0, None, "2021 Bowman Jordan Walker /99",
                     now=1_800_000_000)
    mail(con, "900000000007", 20.0, source="gixen", title="2021 Bowman Jordan Walker /99")
    sales.from_mail(con)
    con.execute("INSERT INTO sales (item_id, title, price, shipping, bids, sold_at)"
                " VALUES ('900000000008', '2021 Bowman Jordan Walker /99', 30.0, 1.0, 4, 1)")
    rows = [dict(r) for r in con.execute("SELECT * FROM sales ORDER BY item_id")]
    text = sales.lines(rows).splitlines()
    assert text[0].endswith("entered by hand | 2021 Bowman Jordan Walker /99")
    assert "your Gixen win" in text[1]
    assert "4 bids | " in text[2] and "your" not in text[2]   # the bot's own: as before
