"""The AI pass (the workers' provider chain): services, animals, areas served, a short description, how it is registered
and its head, read from the organisation's own pages and bio. Nothing is kept that the text does not say: every list
item must be one of the fixed values, every place and name must appear in the text, every number in the description
must appear in the text, every service must be named in the text, and the description is at most 40 plain, factual
words in the model's own words with no praise or marketing words (owner, 25 Oct 2026)."""

from __future__ import annotations

import json
import logging
import re

from ..summarise import note_failure, note_success, usable
from .model import Fact

log = logging.getLogger("aw_workers.directory.ai")

ANIMALS = ["dogs", "cats", "cattle", "birds", "wildlife", "equines", "others"]
PROMPT = """You read an Indian animal-welfare organisation's own web pages and social bio, and list only what they state.

Organisation: {name} ({place})
Text from its pages:
\"\"\"{text}\"\"\"

Answer with one JSON object and nothing else:
{{"services": [], "animals": [], "areas_served": [], "description": "", "reg_type": "", "head": ""}}
- services: only from {services}, and only those the text names as something the organisation does. Never add one
  the text does not mention.
- animals: only from {animals}, only those the text says it helps.
- areas_served: places (cities, districts, states) the text says it serves; [] if not stated.
- description: at most 40 words, plain and factual, in your own words: what the organisation does, for which animals,
  where. Only facts in the text above (the organisation's own pages). No praise or marketing words (dedicated,
  passionate, best, leading, first, trusted, tireless, compassionate, committed...), no "we"/"our", no numbers that are
  not in the text, no claims about the future, no services the text does not name. "" if the text says too little.
- reg_type: "Trust", "Society" or "Section 8 company" only if the text says how it is registered, else "".
- head: "Name, Role" of the founder or head only if the text names them with that role, else ""."""


# Praise and marketing words a description never carries (owner, 25 Oct 2026): a description with one is dropped.
PRAISE = re.compile(
    r"\b(best|finest|leading|premier|top|renowned|famous|trusted|reputed|award[- ]winning|world[- ]class|unique|"
    r"unparalleled|unmatched|outstanding|exceptional|excellent|remarkable|extraordinary|amazing|incredible|wonderful|"
    r"great|dedicated|passionate|tireless(ly)?|selfless(ly)?|devoted|committed|compassionate|caring|loving|noble|"
    r"inspiring|heart[- ]?warming|tirelessly|strives?|striving|mission|vision|journey|making a difference|"
    r"voice(s)? for the voiceless|furry|fur ?bab(y|ies)|(india[’']?s|the|country[’']?s) (first|only|largest|biggest|oldest))\b",
    re.I,
)


def _json(reply: str) -> dict:
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


def _in(text: str, s: str) -> bool:
    return re.sub(r"\s+", " ", s.lower()).strip() in text


def _named(text: str, slug: str, label: str) -> bool:
    """A service the text names: a word of its label (or slug) of 4+ letters, by its first 5 letters (rescue/rescues)."""
    words = [w for w in re.findall(r"[a-z]{4,}", f"{label} {slug}".lower()) if w not in ("care", "service", "services")]
    return any(w[:5] in text for w in words)


def checked(data: dict, text: str, services: dict) -> dict:
    """Only what the text supports."""
    low = re.sub(r"\s+", " ", text.lower())
    out = {}
    svc = [s for s in data.get("services") or [] if s in services and _named(low, s, services[s])]
    if svc:
        out["services"] = list(dict.fromkeys(svc))
    an = [a for a in data.get("animals") or [] if a in ANIMALS]
    if an:
        out["animals"] = ", ".join(dict.fromkeys(an))
    areas = [a.strip() for a in data.get("areas_served") or [] if isinstance(a, str) and a.strip() and _in(low, a)]
    if areas:
        out["areas_served"] = ", ".join(dict.fromkeys(areas))
    desc = re.sub(r"\s+", " ", str(data.get("description") or "")).strip()
    nums_ok = all(n in low for n in re.findall(r"\d[\d,]*", desc.lower()))
    names = re.findall(r"(?<!^)(?<![.!?] )\b([A-Z][a-z]{2,}(?:\s[A-Z][a-z]+)*)", desc)
    names_ok = all(_in(low, n) for n in names)
    plain = not PRAISE.search(desc) and not re.search(r"\b(we|our|us)\b", desc, re.I)
    if desc and len(desc.split()) <= 40 and nums_ok and names_ok and plain:
        out["description"] = desc
    rt = str(data.get("reg_type") or "")
    if rt in ("Trust", "Society", "Section 8 company") and re.search(r"\b(trust|society|section\s?8)\b", low):
        out["reg_type"] = rt
    head = str(data.get("head") or "").strip()
    if head and "," in head and _in(low, head.split(",")[0]):
        out["head"] = head
    return out


def run(ctx, summariser, services: dict) -> None:
    """Adds AI-read facts, credited to the pages the text came from."""
    if not ctx.texts or summariser is None:
        ctx.status["ai"] = "skipped: no text" if not ctx.texts else "skipped: no AI provider"
        return
    text = "\n\n".join(t for _, _, t in ctx.texts)[:9000]
    place = ", ".join(x for x in (ctx.listing.get("district"), ctx.listing.get("state")) if x)
    prompt = PROMPT.format(name=ctx.name, place=place, text=text, services=list(services), animals=ANIMALS)
    reply = ""
    for i, b in enumerate(summariser.backends):
        if not usable(summariser, i):
            continue
        try:
            reply = b.ask(prompt)
            note_success(summariser, i)
            if _json(reply):
                break
        except Exception as exc:  # noqa: BLE001
            note_failure(summariser, i, exc)
    data = checked(_json(reply), text, services)
    if not data:
        ctx.status["ai"] = "nothing usable"
        return
    key, url = ctx.texts[0][0], ctx.texts[0][1]
    name = "The organisation's website" if key == "website" else "Instagram / Facebook"
    for field, value in data.items():
        ctx.add(Fact(field, value, key, name, url, note="read by AI from the organisation's text"))
    ctx.status["ai"] = "ok"
