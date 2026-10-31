"""Claude's replies are read by a tolerant parser: a stray quote in a title, a
trailing comma or a // remark no longer costs an hour's scoring."""
import json

import pytest

from snipehunt import score


def test_a_plain_reply_parses_as_before():
    assert score.extract_json('Here you go:\n```json\n{"picks": [1, 2]}\n```') == {"picks": [1, 2]}


def test_an_inch_mark_inside_a_title_is_kept_as_text():
    raw = '{"picks": [{"item_id": "1", "title": "2023 Topps 8" x 10" photo", "max": 3.03}]}'
    with pytest.raises(ValueError):
        json.loads(raw)
    out = score.extract_json(raw)
    assert out["picks"][0]["title"] == '2023 Topps 8" x 10" photo'
    assert out["picks"][0]["max"] == 3.03


def test_a_trailing_comma_and_a_remark_are_dropped():
    raw = '{"values": [{"item_id": "9", "value": 12.5,}, // the only sale\n ], }'
    assert score.extract_json(raw) == {"values": [{"item_id": "9", "value": 12.5}]}


def test_a_slash_inside_a_string_is_not_a_remark():
    raw = '{"note": "see https://example.com/x // really", "n": 1,}'
    assert score.extract_json(raw)["note"] == "see https://example.com/x // really"


def test_what_cannot_be_mended_raises_the_original_error():
    with pytest.raises(ValueError, match="Unterminated"):
        score.extract_json('{"picks": [{"item_id": "1", "title": "cut off}')
    with pytest.raises(ValueError, match="no JSON object"):
        score.extract_json("sorry, nothing here")


def test_a_bad_reply_is_asked_once_more(monkeypatch, capsys):
    replies = iter(['{"picks": [{"a": 1 "b": 2}]}', '{"picks": []}'])
    calls = []
    monkeypatch.setattr(score, "ask_claude",
                        lambda prompt, tools="", timeout=0: (calls.append(prompt), next(replies))[1])
    assert score.ask_json("hi", tools="") == {"picks": []}
    assert calls == ["hi", "hi"]
    assert "asking again" in capsys.readouterr().err


def test_a_second_bad_reply_fails_and_an_outage_is_not_retried(monkeypatch):
    monkeypatch.setattr(score, "ask_claude", lambda *a, **k: "{bad")
    with pytest.raises(ValueError):
        score.ask_json("hi", tools="")
    n = []
    def down(*a, **k):
        n.append(1)
        raise score.ClaudeError("usage limit")
    monkeypatch.setattr(score, "ask_claude", down)
    with pytest.raises(score.ClaudeError):
        score.ask_json("hi", tools="")
    assert len(n) == 1
