"""Rainbows: every parallel of a card, which are owned and which are for sale
(data/rainbow.md; optional, without it there are none).

A parallel is told apart by its color and finish words ("green", "wave",
"geometric"...). A listing or inventory row is that parallel only when it
carries exactly those words: a Green Wave /99 is not the Green /99, and a plain
Refractor has none of them. Nothing here asks Claude.
"""
from __future__ import annotations

import re

from . import config, musthave, rarity

FILE = config.DATA_DIR / "rainbow.md"
# Spellings sellers use for the same thing, folded to one word before matching.
SAME = [(r"x[\s-]?fractor", "xfractor"), (r"ray[\s-]?wave", "raywave"),
        (r"super[\s-]?fractor", "superfractor"), (r"logo[\s-]?fractor", "logofractor"),
        (r"red,? white,? (?:&|and) blue|\brwb\b", "rwb"), (r"baseball seams", "seams"),
        (r"rose gold", "rosegold"), (r"sky blue", "skyblue"), (r"tie[\s-]?dye", "tiedye"),
        (r"mini diamond", "minidiamond"), (r"\bgeo\b", "geometric"),
        (r"printing plates?", "plate"), (r"\bsparkle\b", "sparkles")]
# The words that make one parallel differ from another.
VARIANT = frozenset("""xfractor prism negative pink sepia aqua blue green purple gold white orange
black red yellow rosegold skyblue magenta wave raywave geometric seams rwb logofractor speckle
sonar lava mojo asia sparkles sapphire superfractor minidiamond tiedye plate""".split())


def _fold(text: str) -> str:
    text = (text or "").lower()
    for pattern, word in SAME:
        text = re.sub(pattern, word, text)
    return text


def variants(text: str) -> tuple[frozenset, bool]:
    """(the variant words in a title or parallel name, does it say refractor)."""
    words = set(re.findall(r"[a-z0-9]+", _fold(text)))
    return frozenset(words & VARIANT), "refractor" in words


def same_parallel(name: str, text: str) -> bool:
    """Is `text` (a listing title or an inventory row's parallel) this parallel?"""
    want, _ = variants(name)
    have, refractor = variants(text)
    if name.strip().lower() == "base":
        return not have and not refractor
    if name.strip().lower() == "refractor":
        return not have and refractor
    return have == want


def read(path=None) -> list[dict]:
    """The rainbows in file order: card, search, owned_when and its parallels."""
    path = path or FILE
    if not path.exists():
        return []
    out, card = [], None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("## "):
            card = {"card": line[3:].strip(), "search": "", "owned_when": "", "parallels": []}
            year = re.search(r"\b(19|20)\d\d\b", card["card"])
            card["year"] = year.group(0) if year else ""
            out.append(card)
        elif card and line.lower().startswith("search:"):
            card["search"] = line.split(":", 1)[1].strip()
        elif card and line.lower().startswith("owned when:"):
            card["owned_when"] = line.split(":", 1)[1].strip()
        elif card and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells[0] in ("Parallel", "") or re.fullmatch(r"[-: ]*", cells[0]):
                continue
            run = cells[1] if len(cells) > 1 else ""
            card["parallels"].append({"name": cells[0],
                                      "run": int(run) if run.isdigit() else None,
                                      "where": cells[2] if len(cells) > 2 else ""})
    return out


def _rows_for(card: dict, rows: list[dict]) -> list[dict]:
    """Inventory rows of this card, any parallel (the card's "Owned when" rule)."""
    return musthave.owners({"year": card["year"], "owned_when": card["owned_when"] + ";"}, rows)


def view(rows: list[dict], listings: list[dict], picks: list[dict]) -> list[dict]:
    """Each rainbow with every parallel's owned row, copies for sale now, the
    cheapest, and a live pick. `listings`: {"title", "total"}; `picks` have titles."""
    out = []
    for card in read():
        mine = _rows_for(card, rows)
        live = [x for x in listings if x.get("title") and musthave.matches(card["search"], x["title"])]
        hunted = [p for p in picks if p.get("title") and musthave.matches(card["search"], p["title"])]
        parallels = []
        for par in card["parallels"]:
            def fits(title: str) -> bool:
                run = rarity.print_run(title)
                return same_parallel(par["name"], title) and (run is None or run == par["run"])
            owned = next((r for r in mine if same_parallel(par["name"], r.get("parallel") or "Base")
                          ), None)
            selling = [x["total"] for x in live if fits(x["title"])]
            pick = next((p for p in hunted if fits(p["title"])), None)
            parallels.append({
                "name": par["name"], "run": par["run"], "where": par["where"],
                "owned": {"id": owned["id"], "status": owned.get("status")} if owned else None,
                "for_sale": len(selling), "cheapest": round(min(selling), 2) if selling else None,
                "pick": {"item_id": pick["item_id"], "tier": pick.get("tier"),
                         "total": pick.get("total"), "url": pick.get("url")} if pick else None,
                "url": musthave.ebay_url(f"{card['search']} {par['name']}"
                                         + (f" /{par['run']}" if par["run"] else "")),
            })
        out.append({"card": card["card"], "parallels": parallels,
                    "owned": sum(bool(p["owned"]) for p in parallels)})
    return out
