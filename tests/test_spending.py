"""Spending history and the forecast of what the collector will pay next."""
from datetime import date

from snipehunt import config, db, report, spending


def row(day, paid, **kw):
    return {"order_date": day, "price_paid": str(paid), "auto": "", "graded": "", "serial": "",
            "rc": "", "status": "In binder", **kw}


ROWS = ([row(f"2026-07-{d:02d}", 4.0) for d in range(1, 11)]           # July: 10 cards, $40
        + [row(f"2026-08-{d:02d}", 6.0) for d in range(1, 11)]          # August: $60
        + [row(f"2026-09-{d:02d}", 8.0, auto="Y") for d in range(1, 11)]  # September: $80
        + [row("2026-10-02", 20.0, graded="PSA 9"), row("2026-10-05", 10.0, serial="12/99"),
           row("", 99.0), row("2026-10-06", "")])                       # no date / no price


def test_months_pace_and_forecast():
    s = spending.summary(ROWS, date(2026, 10, 10), budget=100.0)
    assert s["purchases"] == 32 and s["spent"] == 210.0
    months = {m["month"]: m["spent"] for m in s["months"]}
    assert months["2026-07"] == 40.0 and months["2026-09"] == 80.0
    assert s["this_month"] == {"spent": 30.0, "cards": 2, "pace": 93.0}
    # newest months weigh most: (40*1 + 60*2 + 80*3) / 6
    assert s["forecast"]["next_month"] == 66.67
    assert s["forecast"]["cards_next_month"] == 10
    assert s["budget_left"] == 70.0 and s["over_pace"] is False


def test_usual_price_per_kind_of_card():
    s = spending.summary(ROWS, date(2026, 10, 10), budget=0)
    assert s["by_kind"]["auto"]["usual"] == 8.0 and s["by_kind"]["base"]["n"] == 20
    assert "graded" not in s["by_kind"]       # one graded card isn't a habit yet
    assert spending.usual_for(s, "2024 Topps Chrome Winn auto")["usual"] == 8.0
    assert "budget_left" not in s


def test_no_forecast_without_two_full_months():
    s = spending.summary([row("2026-10-02", 5.0)], date(2026, 10, 10), budget=0)
    assert s["forecast"] is None and s["next_card"]["usual"] == 5.0
    assert spending.summary([], date(2026, 10, 10), budget=0) == {"purchases": 0}


def test_spending_is_in_the_weekly_report():
    config.INVENTORY.write_text(
        "id,year,set,card,parallel,card_no,serial,rc,auto,relic,graded,status,price_paid,order_date\n"
        + "".join(f"{i},2023,Topps,Base,Base,{i},,,,,,In binder,5.00,2026-0{m}-0{i}\n"
                  for m in (8, 9) for i in range(1, 4)))
    text, _ = report.build(db.connect())
    assert "## Spending" in text and "Your next card will likely cost about **$5.00**" in text
