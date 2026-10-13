"""eBay Browse API search for the player's cards.

Read-only: this module only ever calls the search endpoint. It never bids,
buys, makes offers or messages sellers.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import requests

from . import config, profile, settings

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
ALL_FORMATS = "buyingOptions:{AUCTION|FIXED_PRICE|BEST_OFFER}"

def queries() -> list[tuple[str, str, str, str | None]]:
    """Every search to run, as (label, q, filter, sort).

    Built from the player profile, plus the sellers and extra searches set in
    Home Assistant. Each one is an eBay API call every hour.
    """
    who = profile.get()
    name = who.search_name
    slug = lambda text: re.sub(r"\W+", "-", text.lower()).strip("-")   # noqa: E731
    out = [("auctions-ending", name, "buyingOptions:{AUCTION}", "endingSoonest"),
           ("newly-listed", name, ALL_FORMATS, "newlyListed")]
    out += [(slug(k) or "numbered", f"{name} {k}", ALL_FORMATS, "newlyListed")
            for k in who.keyword_searches]
    out += [("typo-" + slug(q), q, ALL_FORMATS, "newlyListed") for q in who.typo_searches or []]
    cfg = settings.load()
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
    def __init__(self) -> None:
        self.client_id, self.client_secret = config.require(
            "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
        self.marketplace = config.get("EBAY_MARKETPLACE")
        self.category = config.get("EBAY_CATEGORY")
        self.token_file = config.ensure_state_dir() / "ebay_token.json"
        self.session = requests.Session()
        self.calls = 0

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
        """One HTTP call, retried with backoff on rate limits and 5xx."""
        delay = 2.0
        for attempt in range(5):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                if attempt == 4:
                    raise
                print(f"ebay: {type(exc).__name__}, retry in {delay:.0f}s", file=sys.stderr)
            else:
                if resp.status_code not in (429, 500, 502, 503, 504):
                    if resp.status_code >= 400:
                        raise RuntimeError(
                            f"eBay {resp.status_code} for {url}: {resp.text[:300]}")
                    return resp
                if attempt == 4:
                    raise RuntimeError(f"eBay {resp.status_code} after retries: {url}")
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                print(f"ebay: HTTP {resp.status_code}, retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
        raise RuntimeError("unreachable")

    def search(self, q: str, flt: str, sort: str | None, limit: int = 100) -> list[dict]:
        params = {"q": q, "category_ids": self.category, "filter": flt, "limit": limit}
        if sort:
            params["sort"] = sort
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DUS"
            + (f"%2Czip%3D{config.get('EBAY_ZIP')}" if config.get("EBAY_ZIP") else ""),
        }
        self.calls += 1
        resp = self._request("GET", SEARCH_URL, params=params, headers=headers)
        return resp.json().get("itemSummaries", []) or []


def _money(node: dict | None) -> tuple[float | None, str]:
    if not node or node.get("value") is None:
        return None, "USD"
    return float(node["value"]), node.get("currency", "USD")


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
    return Listing(
        item_id=str(item_id),
        title=raw.get("title", "").strip(),
        price=price,
        currency=currency,
        shipping=shipping,
        buying_options=options,
        bid_count=raw.get("bidCount"),
        end_time=end,
        seller=(raw.get("seller") or {}).get("username", ""),
        url=f"https://www.ebay.com/itm/{item_id}",
        image=(raw.get("image") or {}).get("imageUrl"),
    )


def search_all(client: EbayClient | None = None, verbose: bool = False) -> list[Listing]:
    """Run every query, de-duplicate by item number, drop titles that aren't the player."""
    client = client or EbayClient()
    found: dict[str, Listing] = {}
    dropped = 0
    errors: list[str] = []
    todo = queries()
    for label, q, flt, sort in todo:
        try:
            raw_items = client.search(q, flt, sort)
        except Exception as exc:  # one bad query shouldn't sink the whole run
            errors.append(f"{label}: {exc}")
            print(f"ebay: query {label} failed: {exc}", file=sys.stderr)
            continue
        kept = 0
        for raw in raw_items:
            item = parse_item(raw)
            if item is None or item.currency != "USD":
                continue
            verdict = classify(item.title)
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
    if errors and len(errors) == len(todo):
        raise RuntimeError("every eBay query failed: " + errors[0])
    if verbose:
        print(f"  {client.calls} API calls, {len(found)} unique listings, "
              f"{dropped} dropped as someone or something else", file=sys.stderr)
    return list(found.values())


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
