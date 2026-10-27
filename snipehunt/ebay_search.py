"""eBay Browse API search for the player's cards.

Read-only: this module only ever calls the search endpoint and reads single
listings. It never bids, buys, makes offers or messages sellers.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import requests

from . import config, profile, settings, usage

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"
SCOPE = "https://api.ebay.com/oauth/api_scope"
ALL_FORMATS = "buyingOptions:{AUCTION|FIXED_PRICE|BEST_OFFER}"
# Short descriptions carry condition and item-specifics text for free, so every
# search asks for them; it is still one call.
FIELDGROUPS = "EXTENDED"
RATE_RETRIES = 2   # a rate-limited call is tried this many more times, then its lane waits

API_CALLS = 0   # eBay calls this process has made, for the weekly self-review
LANE_CALLS: Counter = Counter()   # the same calls by lane (usage.lane_of), saved by ops.py


class RateLimited(RuntimeError):
    """eBay said too many calls: skip this lane for the rest of the run."""


def _count_call(lane: str = "other") -> None:
    global API_CALLS
    API_CALLS += 1
    LANE_CALLS[lane] += 1


def price_filter(low: float | None = None, high: float | None = None) -> str:
    """eBay's price filter, always with its currency: without priceCurrency eBay
    answers error 12012. The only place a price filter is written."""
    lo = "" if low is None else f"{low:g}"
    hi = "" if high is None else f"{high:g}"
    return f"price:[{lo}..{hi}],priceCurrency:USD"


LOOSE = "loose-"   # label prefix of a search whose results needn't name the player


def queries() -> list[tuple[str, str, str, str | None]]:
    """Every search to run, as (label, q, filter, sort).

    Built from the player profile, plus the sellers and extra searches set in
    Home Assistant. Each one is an eBay API call every hour.
    """
    who = profile.get()
    name = who.search_name
    slug = lambda text: re.sub(r"\W+", "-", text.lower()).strip("-")   # noqa: E731
    if who.mode == "team":
        return team_queries(who, slug)
    out = [("auctions-ending", name, "buyingOptions:{AUCTION}", "endingSoonest"),
           ("newly-listed", name, ALL_FORMATS, "newlyListed")]
    out += [(slug(k) or "numbered", f"{name} {k}", ALL_FORMATS, "newlyListed")
            for k in who.keyword_searches]
    out += [("typo-" + slug(q), q, ALL_FORMATS, "newlyListed") for q in who.typo_searches or []]
    cfg = settings.load()
    out += [("extra-" + slug(q), q, ALL_FORMATS, "newlyListed")
            for q in dict.fromkeys(who.extra_searches + cfg["extra_searches"])]
    out += [(LOOSE + slug(q), q, ALL_FORMATS, "newlyListed") for q in who.loose_searches]
    if cfg["sellers"]:
        out.append(("sellers", name, ALL_FORMATS + ",sellers:{"
                    + "|".join(cfg["sellers"]) + "}", "endingSoonest"))
    return out


# Team mode: a team lists ~1,000 cards an hour where a player lists ~15, so its
# main searches take more than one page, and pages are as long as eBay allows.
TEAM_PAGE = 200
TEAM_PAGES = {"auctions-ending": 2, "newly-listed": 2}
TEAM_ROSTER_SEARCHES = 8   # roster players with a search of their own, best first


def team_queries(who, slug) -> list[tuple[str, str, str, str | None]]:
    """A team's searches. The newest listings skip what is under the price floor,
    which is most of them; auctions don't, since a $1 auction can still be a steal."""
    name = who.search_name
    cfg = settings.load()
    floor = "," + price_filter(cfg["team_min_price"])
    out = [("auctions-ending", name, "buyingOptions:{AUCTION}", "endingSoonest"),
           ("newly-listed", name, ALL_FORMATS + floor, "newlyListed")]
    for k in who.keyword_searches:
        label = slug(k) or "numbered"
        out.append((label, f"{name} {k}", ALL_FORMATS, "newlyListed"))
        out.append(("ending-" + label, f"{name} {k}", "buyingOptions:{AUCTION}",
                    "endingSoonest"))
    out += [("roster-" + slug(p), p.lower(), ALL_FORMATS, "newlyListed")
            for p in who.roster[:TEAM_ROSTER_SEARCHES]]
    out += [("typo-" + slug(q), q, ALL_FORMATS, "newlyListed") for q in who.typo_searches or []]
    out += [("extra-" + slug(q), q, ALL_FORMATS, "newlyListed")
            for q in dict.fromkeys(who.extra_searches + cfg["extra_searches"])]
    if cfg["sellers"]:
        out.append(("sellers", name, ALL_FORMATS + ",sellers:{"
                    + "|".join(cfg["sellers"]) + "}", "endingSoonest"))
    return out


@dataclass
class Listing:
    item_id: str            # legacy eBay item number
    title: str
    price: float
    currency: str
    shipping: float | None  # None = not stated in search results
    buying_options: list[str]
    bid_count: int | None
    end_time: str | None    # ISO, in the bot's time zone
    seller: str
    url: str
    image: str | None
    match: str = "yes"      # yes | unclear: is it really the player?
    queries: list[str] = field(default_factory=list)
    seller_feedback_score: int | None = None    # None when eBay didn't say
    seller_feedback_pct: float | None = None    # percent positive, e.g. 99.6
    via: str | None = None      # team mode: how it matched (team, nickname, typo, roster)
    player: str | None = None   # team mode: the roster player named in the title
    category_id: str | None = None   # eBay's leaf category; 261328 is sports card singles
    condition_id: str | None = None  # 2750 graded, 4000 ungraded
    image_count: int | None = None   # the main photo plus additionalImages
    short_description: str = ""      # condition / item-specifics text from the search
    aspects: dict = field(default_factory=dict)   # item specifics, once read with item()
    lane: str = ""                   # "midtier" for the mid-tier lane's listings
    flags: list[str] = field(default_factory=list)    # filters.py warnings (fake_auto, …)
    signals: list[dict] = field(default_factory=list)  # mislabel.py signals that fired
    listed_at: str | None = None     # when the listing first went up (eBay itemOriginDate), ISO UTC
    country: str | None = None       # where it ships from, e.g. "US", "CA"

    @property
    def graded(self) -> bool | None:
        """True for a graded card, False for raw, None when eBay didn't say."""
        return {"2750": True, "4000": False}.get(self.condition_id or "")

    @property
    def total(self) -> float:
        return round(self.price + (self.shipping or 0.0), 2)

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options

    def ends_in_hours(self, now: datetime | None = None) -> float | None:
        if not self.end_time:
            return None
        now = now or datetime.now(timezone.utc)
        return (datetime.fromisoformat(self.end_time) - now).total_seconds() / 3600

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total"] = self.total
        return d


def classify(title: str) -> str:
    """Is this title a card of the player? yes / unclear / no."""
    return profile.get().classify(title)


class EbayClient:
    def __init__(self, con=None) -> None:
        self.client_id, self.client_secret = config.require(
            "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
        self.marketplace = config.get("EBAY_MARKETPLACE")
        # A profile may name its own category: Pokémon cards aren't sports cards.
        # "" means no category at all (lots are listed outside the singles category).
        chosen = profile.get().category
        self.category = config.get("EBAY_CATEGORY") if chosen is None else chosen
        self.token_file = config.ensure_state_dir() / "ebay_token.json"
        self.session = requests.Session()
        self.calls = 0
        self.lane = "other"   # what the next call is for; search_all sets it per query
        self.con = con        # for the item cache; None opens its own connection
        self.attempts = 5     # tries per call on a network error or 5xx; a low lane uses 1

    def _token(self) -> str:
        if self.token_file.exists():
            try:
                cached = json.loads(self.token_file.read_text())
                if cached["expires_at"] - 120 > time.time():
                    return cached["access_token"]
            except (ValueError, KeyError):
                pass
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self._request(
            "POST", TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE})
        body = resp.json()
        body["expires_at"] = time.time() + int(body.get("expires_in", 7200))
        self.token_file.touch(mode=0o600, exist_ok=True)
        self.token_file.chmod(0o600)
        self.token_file.write_text(json.dumps(
            {"access_token": body["access_token"], "expires_at": body["expires_at"]}))
        return body["access_token"]

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """One HTTP call, retried with backoff on 5xx. A rate limit (429, or eBay's
        "call limit" error) gets RATE_RETRIES more tries, then raises RateLimited."""
        missing_ok = kwargs.pop("missing_ok", False)
        delay = 2.0
        limited = 0
        last = self.attempts - 1
        for attempt in range(5):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                if attempt >= last:
                    raise
                print(f"ebay: {type(exc).__name__}, retry in {delay:.0f}s", file=sys.stderr)
            else:
                is_limit = resp.status_code == 429 or (
                    resp.status_code >= 400 and "call limit" in resp.text[:500].lower())
                if is_limit:
                    limited += 1
                    if limited > RATE_RETRIES:
                        raise RateLimited(f"eBay rate limit after {RATE_RETRIES} retries: {url}")
                elif resp.status_code not in (500, 502, 503, 504):
                    if missing_ok and resp.status_code in (400, 404):
                        return resp
                    if resp.status_code >= 400:
                        raise RuntimeError(
                            f"eBay {resp.status_code} for {url}: {resp.text[:300]}")
                    return resp
                if attempt >= last and not is_limit:
                    raise RuntimeError(f"eBay {resp.status_code} after retries: {url}")
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                print(f"ebay: HTTP {resp.status_code}, retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
        raise RuntimeError("unreachable")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DUS"
            + (f"%2Czip%3D{config.get('EBAY_ZIP')}" if config.get("EBAY_ZIP") else ""),
        }

    def _search_json(self, params: dict, lane: str, category: str | None = None) -> dict:
        """The one search call; search() and refinements() both come through here."""
        category = self.category if category is None else category
        if category:
            params["category_ids"] = category
        self.calls += 1
        _count_call(lane)
        return self._request("GET", SEARCH_URL, params=params, headers=self._headers()).json()

    def search(self, q: str, flt: str, sort: str | None, limit: int = 100,
               offset: int = 0, category: str | None = None) -> list[dict]:
        params = {"q": q, "filter": flt, "limit": limit, "fieldgroups": FIELDGROUPS}
        if offset:
            params["offset"] = offset
        if sort:
            params["sort"] = sort
        found = self._search_json(params, usage.lane_of(self.lane), category)
        return found.get("itemSummaries", []) or []

    def refinements(self, q: str) -> list[dict]:
        """eBay's own item-specifics names and values for a search (aspects.py)."""
        body = self._search_json({"q": q, "limit": 1, "fieldgroups": "ASPECT_REFINEMENTS"},
                                 "aspects")
        return (body.get("refinement") or {}).get("aspectDistributions", []) or []

    def item(self, item_id: str, lane: str = "item", fresh: bool = False) -> dict | None:
        """One listing by its item number, ended or not. None once eBay has dropped it.

        A live listing read today is answered from the cache (item_cache.py) instead,
        unless `fresh`: a read that needs this minute's bid and bid count."""
        from . import item_cache   # it opens the database; kept local
        cached = None if fresh else item_cache.get(self.con, item_id)
        if cached is not None:
            return cached
        self.calls += 1
        _count_call(lane)
        resp = self._request("GET", ITEM_URL, params={"legacy_item_id": item_id},
                             headers=self._headers(), missing_ok=True)
        if resp.status_code != 200:
            return None
        body = resp.json()
        item_cache.put(self.con, item_id, body)
        return body


def _money(node: dict | None) -> tuple[float | None, str]:
    if not node or node.get("value") is None:
        return None, "USD"
    return float(node["value"]), node.get("currency", "USD")


def _number(value, kind):
    """eBay's number, or None when it is missing or not a number."""
    try:
        return None if value is None or value == "" else kind(float(value))
    except (TypeError, ValueError):
        return None


def parse_item(raw: dict) -> Listing | None:
    options = raw.get("buyingOptions", [])
    # For auctions the live price is currentBidPrice; `price` is the BIN price.
    price, currency = _money(raw.get("currentBidPrice") if "AUCTION" in options else None)
    if price is None:
        price, currency = _money(raw.get("price"))
    if price is None:
        return None
    shipping = None
    for opt in raw.get("shippingOptions", []) or []:
        shipping, _ = _money(opt.get("shippingCost"))
        if shipping is not None:
            break
    end = raw.get("itemEndDate")
    if end:
        end = (datetime.fromisoformat(end.replace("Z", "+00:00"))
               .astimezone(config.TZ).isoformat(timespec="minutes"))
    item_id = raw.get("legacyItemId") or raw.get("itemId", "").split("|")[1:2][0]
    seller = raw.get("seller") or {}
    category = raw.get("categoryId") or next(
        (c.get("categoryId") for c in raw.get("categories") or [] if c.get("categoryId")), None)
    return Listing(
        item_id=str(item_id),
        title=raw.get("title", "").strip(),
        price=price,
        currency=currency,
        shipping=shipping,
        buying_options=options,
        bid_count=raw.get("bidCount"),
        end_time=end,
        seller=seller.get("username", ""),
        url=f"https://www.ebay.com/itm/{item_id}",
        image=(raw.get("image") or {}).get("imageUrl"),
        seller_feedback_score=_number(seller.get("feedbackScore"), int),
        seller_feedback_pct=_number(seller.get("feedbackPercentage"), float),
        category_id=str(category) if category else None,
        condition_id=str(raw["conditionId"]) if raw.get("conditionId") else None,
        image_count=image_count(raw),
        short_description=str(raw.get("shortDescription") or "")[:600],
        aspects=aspects_of(raw),
        listed_at=raw.get("itemOriginDate") or raw.get("itemCreationDate"),
        country=(raw.get("itemLocation") or {}).get("country"),
    )


def image_count(raw: dict) -> int | None:
    """The main photo plus the extra ones; None when eBay sent no photo fields at all."""
    if "image" not in raw and "additionalImages" not in raw:
        return None
    return (1 if raw.get("image") else 0) + len(raw.get("additionalImages") or [])


def aspects_of(raw: dict) -> dict:
    """Item specifics (name -> value) from a single-listing read; empty for search results."""
    return {str(a["name"]): str(a.get("value", ""))
            for a in raw.get("localizedAspects") or [] if a.get("name")}


def enrich(item: Listing, raw: dict) -> Listing:
    """Fill a search result in from its single-listing read: item specifics, photos."""
    item.aspects = aspects_of(raw) or item.aspects
    item.image_count = image_count(raw) if image_count(raw) is not None else item.image_count
    item.category_id = str(raw.get("categoryId") or item.category_id or "") or None
    item.condition_id = str(raw.get("conditionId") or item.condition_id or "") or None
    item.short_description = item.short_description or str(raw.get("shortDescription") or "")[:600]
    return item


def search_all(client: EbayClient | None = None, verbose: bool = False) -> list[Listing]:
    """Run every query, de-duplicate by item number, drop titles that aren't the player."""
    client = client or EbayClient()
    found: dict[str, Listing] = {}
    dropped = 0
    errors: list[str] = []
    todo = queries()
    who = profile.get()
    team = who.mode == "team"
    if team:   # players already found on another team's cards are ruled out for free
        from . import db, score   # both import this module; kept local
        who.learn(json.loads(db.kv_get(db.connect(), score.NOT_TEAM_KEY, "[]")))
    skipped = []
    for label, q, flt, sort in todo:
        if not usage.ebay_allowed(usage.lane_of(label)):
            skipped.append(label)   # over the day's share of eBay calls: the least useful wait
            continue
        client.lane = label
        try:
            raw_items = (_pages(client, label, q, flt, sort) if team
                         else client.search(q, flt, sort))
        except RateLimited as exc:   # this lane waits for the next run
            errors.append(f"{label}: {exc}")
            print(f"ebay: {label} skipped this run: {exc}", file=sys.stderr)
            continue
        except Exception as exc:  # one bad query shouldn't sink the whole run
            errors.append(f"{label}: {exc}")
            print(f"ebay: query {label} failed: {exc}", file=sys.stderr)
            continue
        kept = 0
        for raw in raw_items:
            item = parse_item(raw)
            if item is None or item.currency != "USD":
                continue
            if team:
                verdict, item.via, item.player = who.match(item.title)
            else:
                verdict = classify(item.title)
                if verdict == "no" and label.startswith(LOOSE) and not who.wrong(item.title):
                    verdict = "unclear"   # no name in the title: Claude judges it
            if verdict == "no":
                dropped += 1
                continue
            item.match = verdict
            existing = found.setdefault(item.item_id, item)
            if label not in existing.queries:
                existing.queries.append(label)
            kept += 1
        if verbose:
            print(f"  {label:16} {len(raw_items):3} results, {kept:3} kept", file=sys.stderr)
    if skipped:
        print(f"ebay: daily call budget: skipped {', '.join(skipped)}", file=sys.stderr)
    if errors and len(errors) == len(todo) - len(skipped):
        raise RuntimeError("every eBay query failed: " + errors[0])
    if verbose:
        print(f"  {client.calls} API calls, {len(found)} unique listings, "
              f"{dropped} dropped as someone or something else", file=sys.stderr)
    return list(found.values())


BACKFILL_PAGES = 10   # pages of TEAM_PAGE listings: 2,000 a search at most


def backfill_search(client: EbayClient | None = None, pages: int = BACKFILL_PAGES,
                    skip_category: str | None = None,
                    verbose: bool = False) -> list[Listing]:
    """Everything already for sale, not just the newest: the player's name and each
    loose search, a page at a time. The hourly searches read only the first 100
    results each, which is all that's new in an hour, but not what was listed
    before the bot started. `skip_category` leaves out one eBay category, for
    example 261328 to sweep only the memorabilia. Player mode only."""
    client = client or EbayClient()
    who = profile.get()
    if who.mode == "team":
        raise ValueError("backfill is for player mode; a team lists far too much")
    slug = lambda text: re.sub(r"\W+", "-", text.lower()).strip("-")   # noqa: E731
    todo = [("backfill", who.search_name)] + [(LOOSE + slug(q), q) for q in who.loose_searches]
    found: dict[str, Listing] = {}
    for label, q in todo:
        client.lane = label
        kept = seen = 0
        for page in range(pages):
            got = client.search(q, ALL_FORMATS, None, limit=TEAM_PAGE, offset=page * TEAM_PAGE)
            if not got:   # past the end. A short page isn't: eBay sometimes sends 199
                break
            seen += len(got)
            for raw in got:
                item = parse_item(raw)
                if item is None or item.currency != "USD":
                    continue
                if skip_category and item.category_id == skip_category:
                    continue
                verdict = classify(item.title)
                if verdict == "no" and label.startswith(LOOSE) and not who.wrong(item.title):
                    verdict = "unclear"
                if verdict == "no":
                    continue
                item.match = verdict
                existing = found.setdefault(item.item_id, item)
                if label not in existing.queries:
                    existing.queries.append(label)
                kept += 1
        if verbose:
            print(f"  {label:16} {seen:5} results, {kept:5} kept", file=sys.stderr)
    if verbose:
        print(f"  {client.calls} API calls, {len(found)} unique listings", file=sys.stderr)
    return list(found.values())


def _pages(client: EbayClient, label: str, q: str, flt: str, sort: str | None) -> list[dict]:
    """A team search, a page at a time; stops early when a page comes back short."""
    out: list[dict] = []
    for page in range(TEAM_PAGES.get(label, 1)):
        got = client.search(q, flt, sort, limit=TEAM_PAGE, offset=page * TEAM_PAGE)
        out += got
        if len(got) < TEAM_PAGE:
            break
    return out


def _fmt(item: Listing) -> str:
    ship = "ship ?" if item.shipping is None else (
        "free ship" if item.shipping == 0 else f"+{item.shipping:.2f}")
    kind = "AUC" if item.is_auction else ("BO " if "BEST_OFFER" in item.buying_options else "BIN")
    end = ""
    if item.end_time and item.is_auction:
        end = datetime.fromisoformat(item.end_time).strftime("%a %-m/%-d %-I:%M%p")
    bids = f"{item.bid_count}b" if item.bid_count is not None else ""
    flag = "?" if item.match == "unclear" else " "
    return (f"{flag}{kind} ${item.price:>7.2f} {ship:>9} {bids:>4} {end:<17} "
            f"{item.item_id:<13} {item.seller[:16]:<16} {item.title[:80]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Search eBay for the player's cards")
    ap.add_argument("--dry-run", action="store_true", help="print results only (default)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--limit", type=int, default=60, help="rows to print")
    args = ap.parse_args()
    items = search_all(verbose=True)
    items.sort(key=lambda i: (i.end_time is None or not i.is_auction, i.end_time or ""))
    if args.json:
        print(json.dumps([i.to_dict() for i in items], indent=1))
        return
    for item in items[: args.limit]:
        print(_fmt(item))
    auctions = sum(i.is_auction for i in items)
    unclear = sum(i.match == "unclear" for i in items)
    print(f"\n{len(items)} listings ({auctions} auctions, {unclear} unclear '?' "
          f"left for Claude to judge)")


if __name__ == "__main__":
    main()
