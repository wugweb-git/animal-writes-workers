"""Leads from Gemini with Google Search grounding (owner, 21 Oct 2026: "ask Gemini for a cohort").

A small group of listings goes in as names and places; back come only the organisation's OWN website, Instagram and
Facebook pages, each with the search results it was grounded on. Nothing else from the model is kept: a lead is only
followed (website.py checks the pages name the organisation; social.py reads the public preview), so a wrong answer
costs one wasted fetch, never a wrong fact. Aggregators (Justdial, ngobase ...) and Google's own pages are not leads."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

log = logging.getLogger("aw_workers.directory.leads")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]
BATCH = 6
SKIP = re.compile(r"justdial|sulekha|indiamart|tripadvisor|wikipedia|ngobase|ngo\.org|myngos|worldsearch|indiancattle|jainhelpline|"
                  r"google\.|gstatic|awbi\.gov|animalwrites|scribd|linkedin|youtube|twitter|x\.com|wa\.me|linktr\.ee|ngodarpan|"
                  r"guidestar|csrbox|giveindia|ketto|milaap|zaubacorp|tofler|instafinancials", re.I)
PROMPT = """For each Indian animal-welfare organisation below, search the web and find the organisation's OWN official
website, Instagram page and Facebook page. Only pages run by that organisation itself (not directories, news, listings
or a different organisation with a similar name). If you cannot find one for sure, leave it "". Do not guess.

{rows}

Answer with one JSON array and nothing else: [{{"id": 1, "website": "", "instagram": "", "facebook": ""}}]"""


def clean(kind: str, url: str) -> str:
    url = (url or "").strip()
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return ""
    if kind == "website":
        return "" if SKIP.search(host) or re.search(r"instagram\.|facebook\.|fb\.com", host) else f"{u.scheme}://{u.netloc}".replace("http://", "https://") if u.path in ("", "/") else f"https://{u.netloc}"
    path = [p for p in u.path.split("/") if p]
    if kind == "instagram" and host == "instagram.com" and path and path[0] not in ("p", "reel", "reels", "explore", "accounts"):
        return f"https://www.instagram.com/{path[0]}/"
    if kind == "facebook" and host in ("facebook.com", "fb.com", "m.facebook.com") and path and path[0] not in ("sharer", "share", "login", "groups", "events", "public", "watch", "photo"):
        return f"https://www.facebook.com/{path[0]}" if path[0] != "p" or len(path) < 2 else f"https://www.facebook.com/p/{path[1]}"
    return ""


def parse(reply: str, ids: set[int]) -> dict[int, dict]:
    m = re.search(r"\[.*\]", reply or "", re.S)
    try:
        rows = json.loads(m.group(0)) if m else []
    except ValueError:
        return {}
    out = {}
    for r in rows:
        try:
            i = int(r.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if i in ids:
            leads = {k: clean(k, r.get(k, "")) for k in ("website", "instagram", "facebook")}
            out[i] = {k: v for k, v in leads.items() if v}
    return out


def find(listings: list[dict], keys: list[str], http) -> dict[int, dict]:
    """{listing id: {website, instagram, facebook}} for listings with nothing to start from; {} when no Gemini key works."""
    found: dict[int, dict] = {}
    state = {"no_search": False}
    keys = [k.strip() for v in keys for k in re.split(r"[\n,]", v or "") if k.strip()]
    todo = [L for L in listings if L.get("name")]
    for i in range(0, len(todo), BATCH):
        group = todo[i:i + BATCH]
        rows = "\n".join(f'{L["id"]}. {L["name"]} ({", ".join(x for x in (L.get("district"), L.get("state")) if x)})' for L in group)
        base = {"contents": [{"parts": [{"text": PROMPT.format(rows=rows)}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": 1500}}
        done = False
        for key in keys:
            for model, grounded in [(m, g) for g in (True, False) for m in MODELS if g or not state["no_search"]]:
                body = {**base, "tools": [{"google_search": {}}]} if grounded and not state["no_search"] else base
                try:
                    r = http.post(URL.format(model=model), gap=4.0, json=body, timeout=90, headers={"x-goog-api-key": key})
                except Exception as exc:  # noqa: BLE001
                    log.warning("lead finder %s: %s", model, type(exc).__name__)
                    continue
                if r.status_code != 200:
                    log.warning("lead finder %s%s: HTTP %s %s", model, " (search)" if grounded else "", r.status_code, r.text[:80].replace("\n", " "))
                    if grounded and r.status_code in (400, 403, 429):
                        state["no_search"] = True  # this key has no Search grounding: ask from the model's own knowledge
                    continue
                parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                found.update(parse("".join(p.get("text", "") for p in parts), {L["id"] for L in group}))
                done = True
                break
            if done:
                break
        if not done and i == 0:
            break  # no key works: stop asking
    return found
