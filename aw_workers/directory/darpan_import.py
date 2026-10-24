"""Import NGO Darpan CSVs the owner downloads (animal-welfare category, state by state) into the enrichment review queue.

  python -m aw_workers.directory.darpan_import data/darpan/haryana.csv [more.csv]
  env: WP_BASE_URL, WP_INGEST_USER, WP_INGEST_APP_PASSWORD, DRY_RUN (default true), REPORT_DIR (darpan-report)

Darpan's own site is not read (its search asks for a CAPTCHA); only files the owner downloaded are used. Each row is
matched to a listing of the SAME STATE by name (similarity 0.8 or more; 0.9 and over counts as high confidence) and, when
both the row and the listing name a district, the district must be the same place. A row that matches two listings, or
none, is reported and skipped. What the row says (Darpan ID, registration number, registered-as, phone, email, website)
goes to Directory -> Enrichment review as a proposal with its source (NGO Darpan, the file's name, the date); nothing is
applied by the import, and the listing is not marked checked. Existing values are never replaced here: the review decides.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path

from .. import ingest
from .model import fold, name_similarity, norm_phone, norm_url, now_ist

log = logging.getLogger("aw_workers.directory.darpan")
URL = "https://ngodarpan.gov.in/#/search-ngo"
COLS = {
    "name": ["ngo name", "name of ngo", "name of the ngo", "name of npo", "npo name", "ngo_name", "organisation name", "organization name", "name"],
    "darpan_id": ["darpan id", "darpan_id", "unique id", "ngo unique id", "unique_id", "ngo id", "npo id", "ngo darpan id"],
    "reg_no": ["registration no", "registration number", "registration no.", "reg no", "reg. no.", "reg_no", "registration no. of the npo"],
    "reg_type": ["registration type", "type of registration", "ngo type", "type of npo", "npo type", "type", "registered as"],
    "state": ["state", "registered state", "npo registered state", "state name"],
    "district": ["district", "registered district", "npo registered district", "district name"],
    "phone": ["mobile", "mobile no", "mobile number", "phone", "phone no", "contact number", "contact no", "telephone", "mobile no."],
    "email": ["email", "email id", "e-mail", "email address", "e-mail id"],
    "website": ["website", "web site", "url", "website url"],
}
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower().lstrip("﻿"))


def read_rows(path: Path) -> list[dict]:
    """The file's rows as {name, darpan_id, reg_no, reg_type, state, district, phone, email, website} (missing = '')."""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(raw.splitlines(), dialect=dialect)
    heads = {norm_header(h): h for h in (reader.fieldnames or [])}
    pick = {k: next((heads[c] for c in cands if c in heads), None) for k, cands in COLS.items()}
    if not pick["name"] or not pick["state"]:
        raise SystemExit(f"{path.name}: needs an NGO name column and a state column (found: {', '.join(heads)})")
    out = []
    for r in reader:
        out.append({k: re.sub(r"\s+", " ", (r.get(col) or "")).strip() if col else "" for k, col in pick.items()})
    return [r for r in out if r["name"]]


def same_place(a: str, b: str) -> bool:
    """Two district names for the same place (one contains the other once folded); '' on either side is no objection."""
    fa, fb = fold(a).strip(), fold(b).strip()
    return not fa or not fb or fa in fb or fb in fa or name_similarity(a, b) >= 0.8


def reg_type_of(text: str) -> str:
    t = (text or "").lower()
    if "section 8" in t or "section-8" in t or "companies act" in t:
        return "Section 8 company"
    if "trust" in t:
        return "Trust"
    if "society" in t or "societies" in t:
        return "Society"
    return ""


def match(row: dict, listings: list[dict]) -> tuple[str, dict | None, float]:
    """('ok'|'none'|'ambiguous'|'other district', listing, similarity)."""
    scored = sorted(((name_similarity(row["name"], L["name"]), L) for L in listings), key=lambda x: -x[0])
    good = [(s, L) for s, L in scored if s >= 0.8]
    if not good:
        return "none", None, scored[0][0] if scored else 0.0
    inside = [(s, L) for s, L in good if same_place(row["district"], L["district"])]
    if not inside:
        return "other district", good[0][1], good[0][0]
    if len(inside) > 1 and inside[0][0] - inside[1][0] < 0.05:
        return "ambiguous", inside[0][1], inside[0][0]
    return "ok", inside[0][1], inside[0][0]


def proposals(row: dict, listing: dict, sim: float, source: dict) -> list[dict]:
    conf = "high" if sim >= 0.9 else "medium"
    out = []

    def add(field, value):
        if value:
            out.append({"field": field, "value": value, "confidence": conf, "sources": [source]})

    if row["darpan_id"] and row["darpan_id"] != listing.get("darpan_id"):
        add("darpan_id", row["darpan_id"])
    add("registration", row["reg_no"])
    add("reg_type", reg_type_of(row["reg_type"]))
    add("phone", norm_phone(row["phone"]))
    if EMAIL.match(row["email"]):
        add("email", row["email"].lower())
    if re.search(r"\.[a-z]{2,}", row["website"], re.I) and "." in row["website"]:
        add("website", norm_url(row["website"]))
    return out


def main(argv=None, env=None) -> int:
    env = dict(env or os.environ)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    files = [Path(a) for a in (argv if argv is not None else sys.argv[1:])]
    if not files:
        print(__doc__)
        return 2
    dry = str(env.get("DRY_RUN", "true")).lower() not in ("0", "false", "no")
    client = ingest.Client(env["WP_BASE_URL"], env["WP_INGEST_USER"], env["WP_INGEST_APP_PASSWORD"])
    stats, per_state, lines = Counter(), {}, []
    index: dict[str, list[dict]] = {}
    for f in files:
        rows = read_rows(f)
        source = {"key": "darpan", "name": "NGO Darpan", "url": URL, "fetched_at": now_ist()}
        for r in rows:
            st = r["state"].strip()
            if st not in index:
                items, page = [], 1
                while True:
                    got = client._call("GET", "/enrich/index", params={"state": st, "page": page}, timeout=120).get("items") or []
                    items += got
                    if len(got) < 1000:
                        break
                    page += 1
                index[st] = items
            stats["rows"] += 1
            kind, L, sim = match(r, index[st])
            stats[kind] += 1
            per_state.setdefault(st, Counter())[kind] += 1
            if kind != "ok":
                lines.append(f"- {kind}: {r['name']} ({r['district']}, {st}){' ~ ' + L['name'] if L else ''}")
                continue
            props = proposals(r, L, sim, {**source, "name": f"NGO Darpan (file {f.name}"+ (f", ID {r['darpan_id']}" if r["darpan_id"] else "") + ")"})
            stats["proposals"] += len(props)
            lines.append(f"- {r['name']} -> [{L['name']}](listing {L['id']}) sim {sim:.2f}: " + (", ".join(p["field"] for p in props) or "nothing new"))
            if props and not dry:
                client._call("POST", "/enrich/results", timeout=120, json={
                    "listing_id": L["id"], "run": f"darpan-{f.stem}", "import": True, "force_review": True, "google_cost": 0,
                    "sources_used": ["darpan"], "proposals": props})
    out = Path(env.get("REPORT_DIR") or "darpan-report")
    out.mkdir(parents=True, exist_ok=True)
    md = [f"# NGO Darpan import {'(dry run) ' if dry else ''}{now_ist()[:16].replace('T', ' ')} IST", "", f"Files: {', '.join(f.name for f in files)}", "",
          f"Rows {stats['rows']} · matched {stats['ok']} · no match {stats['none']} · other district {stats['other district']} · ambiguous {stats['ambiguous']} · proposals {stats['proposals']}", "",
          "| State | Rows | Matched | No match | Other district | Ambiguous |", "|---|---|---|---|---|---|"]
    for st, c in sorted(per_state.items()):
        md.append(f"| {st} | {sum(c.values())} | {c['ok']} | {c['none']} | {c['other district']} | {c['ambiguous']} |")
    md += ["", "## Rows", ""] + lines
    (out / "report.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[:14]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
