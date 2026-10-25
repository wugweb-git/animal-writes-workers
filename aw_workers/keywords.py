"""The site's keyword table (sourcing restructure, phase 2, owner 25 Oct 2026).

From phase 2 the sourcing policy sends one list of keywords (from phase 4, owner 25 Oct 2026, without the old lists
beside it):

    keywords: [{id, label, variants[], roles[], lang, searches[], categories[], sections[], queries[]}]

at the top level and, the same list, as `config.keywords_v2` (so Sources sync copies it into sources.yaml; in a
sources.yaml from before phase 4 `keywords` is the old list of Include words). Roles: include, animal, context, exclude, search, category (the Sourcing roles) and
press. A keyword's spellings are its label plus its variants; a trailing `*` matches the start of a word.

The site's lists are read only with this block (sources.is_v2); a sources.yaml without it is read the old way. From it:
- relevance lists (policy.py) and keyword checks by role: the spellings of the keywords with that role;
- category words: the spellings of `category` keywords, by the category slugs they map to;
- Google News searches: each keyword's `searches`, keeping their feed ids (run.keyword_searches);
- press queries: the `queries` of the `press` keywords (press.run).

Hindi and Hinglish keywords never get a Sourcing role (owner, standing rule). The site sends English records only; a
record with another language is ignored here too, whatever roles it has.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SOURCING_ROLES = ("include", "animal", "context", "exclude", "search", "category")
ROLES = (*SOURCING_ROLES, "press")
_warned: set[str] = set()


def _warn_once(msg: str, *args) -> None:
    key = msg % args
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _is_list_of_keywords(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(k, dict) for k in value)


def raw_block(data: dict | None) -> list | None:
    """The keyword list in a lists dict (`keywords_v2`, or `keywords` when it holds objects, not v1 strings), or in a
    policy response (top-level `keywords`, else `config.keywords_v2`). None when there is none."""
    data = data or {}
    for v in (data.get("keywords_v2"), data.get("keywords")):
        if _is_list_of_keywords(v):
            return v
    config = data.get("config")
    if isinstance(config, dict) and config is not data:
        return raw_block(config)
    return None


def _strs(value) -> list[str]:
    return [str(x).strip() for x in (value or []) if isinstance(x, (str, int, float)) and str(x).strip()]


def from_data(data: dict | None) -> list[dict] | None:
    """The usable keywords: English records with at least one known role, each cleaned to the payload's shape. None when
    the block is absent, or when not one record in it is usable (then the lists are not the site's: sources.yaml is
    read, rather than empty lists)."""
    block = raw_block(data)
    if block is None:
        return None
    out = []
    for k in block:
        label = str(k.get("label") or "").strip()
        roles = [r for r in (str(x).strip().lower() for x in k.get("roles") or []) if r in ROLES]
        if not label or not roles:
            continue
        lang = str(k.get("lang") or "").strip().lower()
        if lang != "en":  # defensive: the site never sends these (Hindi/Hinglish get no Sourcing role)
            _warn_once("keyword %r (lang %s) has roles %s: ignored, only English keywords are used", label, lang or "?", roles)
            continue
        searches = [s for s in k.get("searches") or [] if isinstance(s, dict)]
        out.append({
            "id": k.get("id"), "label": label, "variants": _strs(k.get("variants")), "roles": roles, "lang": lang,
            "searches": searches,
            "categories": _strs(k.get("categories")), "sections": _strs(k.get("sections")),
            "queries": _strs(k.get("queries")),
        })
    if not out:
        _warn_once("the keyword table has %d record(s) and none is usable: these lists are not used", len(block))
        return None
    return out


def spellings(k: dict) -> list[str]:
    return list(dict.fromkeys([k["label"], *k.get("variants", [])]))


def role_words(kws: list[dict], role: str) -> list[str]:
    """Every spelling of the keywords with this role, in payload order, without repeats."""
    words: list[str] = []
    for k in kws:
        if role in k["roles"]:
            words += spellings(k)
    return list(dict.fromkeys(words))


def relevance_lists(kws: list[dict]) -> dict:
    """The four relevance lists (strong, animal, context, exclude), built from the roles."""
    return {"strong": role_words(kws, "include"), "animal": role_words(kws, "animal"),
            "context": role_words(kws, "context"), "exclude": role_words(kws, "exclude")}


def category_words(kws: list[dict], slugs=()) -> dict[str, list[str]]:
    """slug -> spellings of the `category` keywords mapped to it (the site no longer keeps word lists per category). `slugs` (the
    site's category list) come first, so every category stays known even before it has a word."""
    out: dict[str, list[str]] = {str(s): [] for s in slugs or []}
    for k in kws:
        if "category" not in k["roles"]:
            continue
        for slug in k["categories"]:
            out.setdefault(slug, [])
            out[slug] = list(dict.fromkeys([*out[slug], *spellings(k)]))
    return out


def search_specs(kws: list[dict]) -> tuple[list[tuple[dict, dict]], list[tuple[dict, dict]]]:
    """(keyword, search) pairs: first the searches that carry a whole source (`url`: the Google News feeds that became
    keywords, with their own settings), then the plain ones (`q`), each in payload order."""
    whole, plain = [], []
    for k in kws:
        for s in k["searches"]:
            if str(s.get("url") or "").strip():
                whole.append((k, s))
            elif str(s.get("q") or "").strip():
                plain.append((k, s))
    return whole, plain


def press_queries(kws: list[dict]) -> list[str]:
    """The press searches: every `queries` entry of the press keywords (the label when a record has none), in order."""
    out: list[str] = []
    for k in kws:
        if "press" in k["roles"]:
            out += k["queries"] or [k["label"]]
    return list(dict.fromkeys(out))


def press_config(data: dict | None) -> dict:
    """The press settings (`enabled`, `date_floor`, `every_days`) with the queries from the Press role when the keyword
    block is there (any `queries` the press block still carries are not read); a sources.yaml from before phase 2: its
    `press` block as it is."""
    cfg = dict((data or {}).get("press") or {})
    kws = from_data(data)
    if kws is not None:
        cfg["queries"] = press_queries(kws)
    return cfg
