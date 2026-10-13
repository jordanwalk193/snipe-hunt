from snipehunt import config, inventory, inventory_sync


def test_roundtrip_is_byte_identical():
    fields, rows = inventory.read()
    assert inventory.dumps(fields, rows) == config.INVENTORY.read_text()


def test_status_only_moves_forward():
    row = {"status": "In transit"}
    assert not inventory.advance(row, "Won")
    assert inventory.advance(row, "Delivered")
    binder = {"status": "In binder"}
    assert not inventory.advance(binder, "Delivered")


def test_apply_adds_and_updates(monkeypatch):
    fields, rows = inventory.read()
    n = len(rows)
    rows[0]["status"] = "In transit"   # don't depend on the live row's status
    monkeypatch.setattr(inventory, "read", lambda: (fields, rows))
    events = [
        {"type": "won", "is_player": True, "item_id": "111111111111", "price": 6.06,
         "seller": "bigseller", "date": "2026-10-10", "source": "gixen", "match_row": None,
         "row": {"year": "2024", "set": "Topps Chrome", "card": "Base", "parallel": "Sepia"}},
        {"type": "delivered", "is_player": True, "item_id": "111111111111", "date": "2026-10-14"},
        {"type": "delivered", "is_player": True, "item_id": None, "match_row": "1",
         "date": "2026-10-13"},
        {"type": "won", "is_player": False, "item_id": "2", "row": {"year": "1"}},
        {"type": "delivered", "is_player": True, "item_id": "999", "match_row": None},
    ]
    f2, r2, changes, counts = inventory_sync.apply(events)
    assert len(r2) == n + 1 and counts == {"added": 1, "updated": 2, "gixen_wins": 1, "lost": 0}
    assert r2[-1]["status"] == "Delivered" and r2[-1]["id"] == str(n + 1)
    assert r2[0]["status"] == "Delivered"
    assert inventory_sync.commit_message(counts).startswith("inventory: +1 (Gixen wins)")
