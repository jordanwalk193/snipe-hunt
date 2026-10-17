"""The read-only guards, checked on every pull request.

Snipe Hunt only reads and recommends. These tests fail if a change, including
one Claude writes for the monthly improvement pull request, quietly removes
one of the things that keep it that way. See docs/SELF_IMPROVE.md: a change
that needs one of these tests edited is a question for Jordan, not a fix.
"""
import json
import re
import subprocess
from pathlib import Path

from snipehunt import config, score, settings
from snipehunt.ebay_search import Listing

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "snipehunt"


def source(name: str) -> str:
    return (PACKAGE / name).read_text()


def test_claude_only_ever_gets_web_reading_tools(monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "{}"}), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    score.ask_claude("hello")
    score.ask_claude("hello", tools="")
    with_web, without = seen
    for cmd in seen:
        assert "--tools" in cmd and "--strict-mcp-config" in cmd
        assert "--dangerously-skip-permissions" not in cmd
    assert score.WEB_TOOLS == "WebFetch,WebSearch"
    assert with_web[with_web.index("--tools") + 1] == "WebFetch,WebSearch"
    assert with_web[with_web.index("--allowedTools") + 1] == "WebFetch,WebSearch"
    assert without[without.index("--tools") + 1] == "" and "--allowedTools" not in without


def test_no_module_names_a_tool_that_can_act():
    for path in PACKAGE.glob("*.py"):
        text = path.read_text()
        assert not re.search(r"""["'](Bash|Write|Edit|NotebookEdit|Task)["',]""", text), path.name
        assert "skip-permissions" not in text and "bypassPermissions" not in text, path.name
        # Claude is started in exactly one place
        assert path.name in ("score.py", "config.py") or "CLAUDE_BIN" not in text, path.name


def test_the_scoring_prompt_still_says_never_bid_or_buy():
    assert ("HARD RULES: recommend only. Never bid, buy, make offers, message sellers or "
            "touch Gixen. You may only read web pages.") in score.PROMPT
    assert "you never bid, buy or contact anyone" in score.SYSTEM
    assert "never bid" in source("snipecheck.py") and "never bids" in source("reflect.py")


def test_a_pick_priced_past_its_max_is_never_surfaced():
    over = Listing("1", "t", 12.13, "USD", 0.0, ["AUCTION"], 3, None, "s", "u", None)
    at = Listing("2", "t", 12.12, "USD", 0.0, ["AUCTION"], 3, None, "s", "u", None)
    raw = {"tier": "fire", "max": 12.12, "value": 40}
    assert score._pick_dict(over, raw) is None
    assert score._pick_dict(at, raw) is not None


def test_gixen_only_logs_in_and_reads_the_page():
    text = source("gixen.py")
    posts = re.findall(r"\.post\(([^,]+),", text)
    assert posts == ['BASE + "home_1.php"']                      # the login form, nothing else
    assert not re.search(r"\.(put|patch|delete|request)\(", text)
    assert set(re.findall(r"home_\d\.php", text)) == {"home_1.php", "home_2.php"}
    for word in ("addsnipe", "delete_snipe", "snipe_add", "edititem", "maxbid="):
        assert word not in text.lower()


def test_ebay_is_searched_and_read_and_nothing_else():
    text = source("ebay_search.py")
    assert re.findall(r'_request\(\s*"(\w+)", (\w+)', text) == [
        ("POST", "TOKEN_URL"), ("GET", "SEARCH_URL"), ("GET", "ITEM_URL")]
    assert set(re.findall(r"https://api\.ebay\.com/[\w/.]+", text)) == {
        "https://api.ebay.com/identity/v1/oauth2/token",
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
        "https://api.ebay.com/oauth/api_scope"}
    for path in PACKAGE.glob("*.py"):
        lowered = path.read_text().lower()
        for endpoint in ("/buy/offer", "/buy/order", "place_proxy_bid", "/sell/", "/post-order/"):
            assert endpoint not in lowered, (path.name, endpoint)


def test_the_self_review_has_no_tools_and_writes_only_to_github_issues():
    text = source("reflect.py")
    assert 'tools=""' in text and "WEB_TOOLS" not in text
    assert "import subprocess" not in text and "import subprocess" not in source("tune.py")
    assert "requests" not in source("tune.py")
    writes = re.findall(r'_github\(\s*"(POST|PATCH|PUT|DELETE)",\s*f?"([^"]*)"', text)
    assert writes and all(path.startswith("/issues") for _, path in writes)
    assert not re.search(r"requests\.(post|put|patch|delete)\(", text)
    # the only files it writes are its own reports
    assert re.findall(r"(\w+)\.write_text\(", text) == ["path"]
    assert 'path = config.REPORTS_DIR / f"reflect-{week}.md"' in text


def test_self_tuning_can_only_reach_the_listed_tunables():
    assert set(settings.TUNABLES) == {"edge_min_pct", "min_overlap", "track_off",
                                      "price_lookups", "fast_min_price", "yellow_max_total"}
    for name in settings.TUNABLES:
        assert not any(word in name for word in ("quiet", "alert", "gixen", "urgent",
                                                 "bedtime", "token", "pass", "self_tuning"))
    text = source("tune.py")
    assert re.findall(r"settings\.set_value\(con, ([^,]+),", text) == ["name", 'row["name"]']
    assert "if name not in settings.TUNABLES" in text


def test_secrets_are_not_settings_and_the_token_for_issues_is_a_secret():
    assert "GITHUB_TOKEN" in config.SECRETS
    assert not [key for key in settings.DEFAULTS
                if re.search(r"token|password|secret|_pass$|_key$", key)]
    ignored = (ROOT / ".gitignore").read_text().split()
    assert ".env" in ignored and "*.db" in ignored


def test_the_improvement_instructions_keep_their_off_limits_list():
    text = " ".join((ROOT / "docs" / "SELF_IMPROVE.md").read_text().split())
    for must in ("The bot never changes its own code on main",
                 "Don't merge. Don't push to main.",
                 "Off-limits in these PRs: removing or weakening any read-only guard",
                 "anything touching secrets/env handling, CI config, or this `SELF_IMPROVE.md` "
                 "file",
                 "question for Jordan"):
        assert must in text, must
    assert "cat docs/SELF_IMPROVE.md" in (ROOT / "Makefile").read_text()


def test_ci_still_runs_these_tests():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "python -m pytest" in ci and "pull_request" in ci


def test_the_team_prompt_carries_the_same_hard_rules(monkeypatch):
    from snipehunt import profile
    monkeypatch.setattr(profile, "_cache", profile.parse(
        (config.EXAMPLES / "profile.team.example.toml").read_text()))
    text = score.team_prompt()
    assert ("HARD RULES: recommend only. Never bid, buy, make offers, message sellers or "
            "touch Gixen. You may only read web pages.") in text
    # team mode scores with the same Claude call and the same two tools
    assert "ask_claude(" in source("score.py") and "team_prompt" in source("score.py")
