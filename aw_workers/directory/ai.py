"""The AI pass (the workers' provider chain): services, animals, areas served, a short description, how it is registered
and its head, read from the organisation's own pages and bio. Nothing is kept that the text does not say: every list
item must be one of the fixed values, every place and name must appear in the text, every number in the description
must appear in the text, and the description is at most 60 words in the model's own words."""

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
- services: only from {services}, and only those the text says the organisation offers.
- animals: only from {animals}, only those the text says it helps.
- areas_served: places (cities, districts, states) the text says it serves; [] if not stated.
- description: at most 60 words, in your own words, about what the organisation does, using only facts in the text.
  No praise, no numbers that are not in the text, no claims about the future. "" if the text says too little.
- reg_type: "Trust", "Society" or "Section 8 company" only if the text says how it is registered, else "".
- head: "Name, Role" of the founder or head only if the text names them with that role, else ""."""


def _json(reply: str) -> dict:
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


def _in(text: str, s: str) -> bool:
    return re.sub(r"\s+", " ", s.lower()).strip() in text


def checked(data: dict, text: str, services: dict) -> dict:
    """Only what the text supports."""
    low = re.sub(r"\s+", " ", text.lower())
    out = {}
    svc = [s for s in data.get("services") or [] if s in services]
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
    if desc and len(desc.split()) <= 60 and nums_ok and names_ok:
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
