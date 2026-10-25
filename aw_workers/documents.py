"""Library documents: Gazette of India notifications and court judgments, as documents (not news about them).

- egazette:     egazette.gov.in "Recent uploads" (latest 100 Extraordinary and Weekly gazettes, no CAPTCHA). Kept when the
                ministry is one we follow and the subject is about animals. PDF: egazette.gov.in/WriteReadData/<year>/<id>.pdf
- judgments-sc: Supreme Court judgments from the open dataset s3://indian-supreme-court-judgments (CC BY 4.0,
                github.com/vanga/indian-supreme-court-judgments): one metadata file per year.
- judgments-hc: High Court judgments from s3://indian-high-court-judgments (CC BY 4.0): one metadata file per court bench
                and year; final decisions of the right kinds are read (first pages of the PDF) and kept when they cite
                an animal statute or are plainly about animals. See the High Courts section.

Government Acts, rules, notifications and judgments may be reproduced (s.52(1)(q), Copyright Act 1957); the site keeps a
copy of the PDF and credits the source. Interim orders and bail matters are skipped: the Library keeps decisions.
"""

from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .sources import TIMEOUT, USER_AGENT, Item, Source, _clean

STATE_FILE = Path(os.environ.get("AW_DOCS_STATE", ".state/documents.json"))
_state: dict | None = None

# A judgment or notice is about animals when one of these appears (whole words, any case).
ANIMAL_TERMS = re.compile(
    r"\b(prevention of cruelty to animals|cruelty to animals|animal welfare|animal birth control|awbi|"
    r"wild ?life \(protection\)|wildlife protection|stray (?:dog|dogs|animals|cattle)|street dogs?|community dogs?|"
    r"dog bites?|rabies|gau ?shala|go ?shala|gaushalas|cow slaughter|cattle slaughter|bovine|slaughter of (?:cows?|cattle|animals)|"
    r"transport(?:ation)? of (?:cattle|animals)|people for animals|jallikattu|kambala (?:race|festival)|bullock[- ]cart race|"
    r"poaching|tiger reserve|elephant corridor|wild animals?|pet shops?|dog breeding|performing animals|zoo|zoological park|"
    r"laboratory animals|animal (?:cruelty|rights|activists?|rescue|shelters?|feeders?|lovers?))\b",
    re.I,
)
# "Animal Husbandry" alone is a department name (service and staff matters), not an animal-welfare subject.
DEPARTMENT_ONLY = re.compile(r"\banimal\s+hus\w*\.?", re.I)  # husbandry, husbandary, hus., husb.

SC_BUCKET = "https://indian-supreme-court-judgments.s3.ap-south-1.amazonaws.com"
HC_BUCKET = "https://indian-high-court-judgments.s3.ap-south-1.amazonaws.com"
_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"


# ---------- state (High Court file versions, last check) ----------

def state() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            _state = {}
    return _state


def save_state() -> None:
    if _state is not None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(_state))


# ---------- helpers ----------

def about_animals(text: str) -> bool:
    return bool(ANIMAL_TERMS.search(DEPARTMENT_ONLY.sub(" ", text or "")))


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    for fmt, n in (("%d-%m-%Y", 10), ("%Y-%m-%d", 10), ("%d-%b-%Y", 11)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def _get(url: str, **kw) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=kw.pop("timeout", TIMEOUT * 4), **kw)
    resp.raise_for_status()
    return resp


def s3_list(bucket: str, prefix: str, get=_get) -> list[dict]:
    """Every object under a prefix: key, size, etag (anonymous S3 ListObjectsV2)."""
    out, token = [], None
    while True:
        url = f"{bucket}/?list-type=2&prefix={requests.utils.quote(prefix)}"
        if token:
            url += f"&continuation-token={requests.utils.quote(token)}"
        root = ET.fromstring(get(url).content)
        for c in root.findall(_S3 + "Contents"):
            out.append({"key": c.findtext(_S3 + "Key"), "size": int(c.findtext(_S3 + "Size") or 0), "etag": c.findtext(_S3 + "ETag")})
        if root.findtext(_S3 + "IsTruncated") != "true":
            return out
        token = root.findtext(_S3 + "NextContinuationToken")


def read_parquet(content: bytes, columns: list[str]) -> list[dict]:
    import pyarrow.parquet as pq  # only these sources need it

    table = pq.read_table(io.BytesIO(content))
    keep = [c for c in columns if c in table.schema.names]
    return table.select(keep).to_pylist()


def _text(*parts) -> str:
    return " ".join(str(p) for p in parts if p not in (None, "", "None"))


# ---------- Supreme Court ----------

def sc_items(source: Source, rows: list[dict], year: int) -> list[Item]:
    items = []
    for r in rows:
        if not about_animals(_text(r.get("title"), r.get("description"))):
            continue
        when = _date(r.get("decision_date"))
        path = str(r.get("path") or "")
        if not path or not when:
            continue
        cite = str(r.get("nc_display") or r.get("citation") or "").strip()
        title = _clean(str(r.get("title") or "")) + (f" ({cite})" if cite else "")
        pdf = f"{SC_BUCKET}/data/pdf/year={year}/english/{path}_EN.pdf"
        items.append(Item(source, title, pdf, when, _clean(str(r.get("description") or ""))[:4000], "Supreme Court of India · Indian Supreme Court Judgments dataset (CC BY 4.0)",
                          doc_kind="judgment", court="supreme-court", document_url=pdf))
    return sorted(items, key=lambda i: i.date, reverse=True)


def parse_judgments_sc(source: Source, body: str = "", get=_get, today: date | None = None) -> list[Item]:
    today = today or datetime.now(timezone.utc).date()
    items = []
    years = {int(y) for y in os.environ.get("AW_SC_YEARS", "").split(",") if y.strip().isdigit()}  # backfill: several years
    for year in sorted(years or {today.year, (today - timedelta(days=45)).year}):
        try:
            content = get(f"{SC_BUCKET}/metadata/parquet/year={year}/metadata.parquet").content
        except requests.HTTPError:
            continue
        rows = read_parquet(content, ["title", "description", "decision_date", "path", "nc_display", "citation"])
        items += sc_items(source, rows, year)
    return items


# ---------- High Courts ----------
#
# About 1,700 High Court decisions a day are in the dataset, and its metadata says little: the title is the case number
# and the parties, the description the first ~290 characters of the PDF (court header and parties). Metadata alone
# gave mostly false positives (a gaushala in an address, zoo staff service matters), so (owner, 25 Oct 2026):
#   1. the metadata only rules out what is not a final decision or not our subject (bail, interim and miscellaneous
#      criminal applications; service, tax, motor-accident, arbitration, land-acquisition matters...), by the case-type
#      token at the start of the title;
#   2. the judgment itself decides: its first pages must cite an animal statute, or use the animal terms at least twice
#      outside the party and address lines (add_hc_text). An animal-welfare body as a party (AWBI, PFA, PETA...) is
#      enough on its own.
# Each bench keeps a watermark (the last decision date read, and the PDFs read in the HC_GRACE days before it, so a
# decision the dataset adds a few days late is still read), so a changed metadata file is not read again from the start;
# at most HC_MAX_PDFS PDFs (and HC_MINUTES) per run, oldest first, the next run carries on. A PDF that could not be
# downloaded (other than "not found") is tried again next run.

HC_MAX_PDFS = int(os.environ.get("AW_HC_MAX_PDFS") or 1500)
HC_MINUTES = float(os.environ.get("AW_HC_MINUTES") or 8)
HC_PAGES = 3  # the statute and the subject are on the first pages
HC_THREADS = 8
HC_LOOKBACK = 60  # days, when the source sets none
HC_GRACE = 10  # days before the watermark in which late additions are still read
HC_COLUMNS = ["title", "description", "decision_date", "pdf_link", "cnr", "court", "disposal_nature"]

# The case-type token: "BAIL APPLN./123/2026 of X Vs Y" -> "BAIL APPLN."; "WP 123/2026" -> "WP".
_CASE_TYPE = re.compile(r"\s*([A-Za-z][^/]*?)\s*(?:/|\s\d|$)")
# Bail, anticipatory bail, interim and miscellaneous applications, miscellaneous criminal petitions (482 / 528 BNSS).
# Compared upper case without spaces. Not decisions the Library keeps.
HC_MISC_TYPES = re.compile(
    r"BAIL.*|B\.?A\.?|A\.?B\.?A\.?|AB|BLAPL|ABLAPL|NABAIL|CRMBA|CRLMBA|CRLMB|CRLMP|MCRCA|CRM\(A\)|"
    r"CRL\.?M\.?C\.?|CRLMC|CRM-M|CRM\(M\)|CRMM|CRMMO|CR\.?M\.?A\.?|CR\.?M\.?P\.?|CRMP|CRMPM|MCRC|CRL\.?M\.?A\.?|CRL\.?MISC.*|"
    r"NA52[89]|A482|A528|CRM\(NDPS\)|"
    r"I\.?A\.?|I\.?A\.?\(.*|C\.?M\.?P\.?|C\.?M\.?P\.?M|CMPMO|CM\.?APPL\.?.*|APPLN\.?|MA|MCA|MCC|CAN"
)
# Matters that are never about animals: service, tax, motor accident claims, arbitration and commercial, company, land
# acquisition, family, probate, habeas corpus, election, rent, execution.
HC_OFF_TOPIC_TYPES = re.compile(
    r"WPS|SWP|WP\(S\)|S\.?W\.?P\.?|WRIA|WRIB|WP\.?CT|CWPOA|OA|T\.?A\.?|SERVICE.*|"
    r"W\.?TAX|ITA|ITR|TAXAP|STR|GSTR|CUS.*|CEA.*|VAT.*|STA|TAX.*|"
    r"MAC.*|FAFO|"
    r"ARB.*|CARBP|CARAP|O\.?M\.?P\.?.*|OMP.*|COMAP|CS\(COMM\)|AP-COM|COMM.*|COM\.?APP.*|"
    r"CO\.?APPL.*|COMPANY.*|"
    r"LAA.*|LA\.?APP.*|LAAP|RFA\(LA\)|LAC.*|"
    r"MAT\.?APP.*|CRL\.?REV\.?P\.?\(MAT\.?\)|FAM.*|TEST\.?CAS\.?|HABC|HCP|EP|EL\.?PET.*|ELEC.*|RC\.?REV\.?|EX\.?P\.?|EXE.*"
)
# Disposals that decide nothing.
HC_NO_DECISION = re.compile(r"default|non[- ]?prosecution|withdrawn|infructuous|not\s+pressed|abated", re.I)
# A party that is an animal-welfare body: the case is ours whatever the type (bail and interim matters still skipped).
HC_STRONG_PARTIES = re.compile(
    r"\banimal\s+welfare\b|\bAWBI\b|people\s+for\s+animals|\bPETA\b|people\s+for\s+(?:the\s+)?ethical\s+treatment|"
    r"wild\s?life\s+protection\s+society|humane\s+society",
    re.I,
)
# Animal statutes, as cited in a judgment. A Society for the Prevention of Cruelty to Animals is a party, not a statute:
# the Act or Rules must follow.
ANIMAL_STATUTES = re.compile(
    r"prevention\s+of\s+cruelty\s+to\s+animals?\b[^.\n]{0,60}?\b(?:act|rules)\b|\bp\.?\s?c\.?\s?a\.?\s+act\b|"
    r"\bwild\s?-?\s?life\s*\(\s*protection\s*\)|\bwild\s?-?\s?life\s+protection\s+act\b|"
    r"animal\s+birth\s+control|\ba\.?\s?b\.?\s?c\.?\s+rules\b|"
    r"transport\s+of\s+animals?\b[^.\n]{0,30}?\brules\b|performing\s+animals?\b[^.\n]{0,30}?\brules\b|"
    r"\b(?:cow|cattle|bovine|go\s?-?vansh|gau\s?-?vansh|gou\s?-?vansh|animals?|pashu)\b[\w\s,()&-]{0,60}?"
    r"\b(?:slaughter|preservation|protection|sanrakshan|parirakshan|pratishedh)\b[\w\s,()&-]{0,60}?\bact\b|"
    r"animal\s+slaughter\s+control|"
    r"\b42[89]\b[^.\n]{0,25}?(?:\bi\.?\s?p\.?\s?c\b|indian\s+penal\s+code)|\b325\b[^.\n]{0,15}?(?:\bb\.?\s?n\.?\s?s\b|bharatiya\s+nyaya)",
    re.I,
)
# Where the judgment proper starts (court header and parties end).
_HC_BODY = re.compile(r"\bCORAM\b|\bJ\s?U\s?D\s?G\s?M\s?E\s?N\s?T\b|\bO\s?R\s?D\s?E\s?R\b|\bHeard\b|\bPronounced\b")
_PARTY_LINE = re.compile(
    r"\.{3,}|…|\b(?:petitioner|respondent|appellant|applicant|complainant|accused|opposite\s+part(?:y|ies)|"
    r"through|advocates?|adv\.|counsel|versus|vs\.?|r/o|s/o|d/o|w/o|c/o|resident|aged\s+about|village|vill\.|"
    r"post\s+office|p\.o\.|police\s+station|p\.s\.|district|dist\.|tehsil|taluk|mandal|pin|address)\b",
    re.I,
)
_ADDRESS_LINE = re.compile(r"\.{3,}\s*(?:petitioner|respondent|appellant|applicant)|\b(?:r/o|s/o|d/o|w/o|c/o|resident\s+of|aged\s+about|pin\s?(?:code)?\s*[:-]?\s*\d{6})\b", re.I)
# A place named after one (Zoo Road, Gaushala Lane in Guwahati or Kanpur addresses) is not about animals.
_PLACE_NAME = re.compile(r"\b(?:zoo|zoological\s+park|gau\s?shala|go\s?shala)\s+(?:road|lane|bye\s?lane|street|marg|path|nagar|colony|tiniali|chowk|area)\b", re.I)
# A bail order filed under another case type (criminal petitions in Karnataka, Telangana, applications in Bombay...).
_BAIL_TEXT = re.compile(
    r"\b(?:regular|anticipatory)\s+bail\b|\bbail\s+(?:application|petition)\b|\bgrant\s+of\s+bail\b|"
    r"\bseek(?:s|ing)?\s+(?:regular\s+|anticipatory\s+|interim\s+)?bail\b|\bapplication\s+for\s+(?:grant\s+of\s+)?bail\b|"
    r"\bsections?\s+43[89]\b[^.\n]{0,40}(?:cr\.?\s?p\.?\s?c|code\s+of\s+criminal)|\bsections?\s+48[23]\b[^.\n]{0,40}(?:bnss|bharatiya\s+nagarik)",
    re.I,
)


@dataclass
class HCCandidate:
    """A High Court decision that passed the metadata check and needs its PDF read."""

    key: str  # the metadata file
    year: int
    court_dir: str
    bench: str
    title: str
    court: str
    when: date
    pdf: str  # basename
    strong: bool = False

    @property
    def url(self) -> str:
        return f"{HC_BUCKET}/data/pdf/year={self.year}/court={self.court_dir}/bench={self.bench}/{self.pdf}"

    @property
    def order(self) -> tuple:
        return (self.when, self.pdf)


def case_type(title: str) -> str:
    m = _CASE_TYPE.match(title or "")
    return re.sub(r"\s+", "", m.group(1)).upper() if m else ""


def hc_skip_reason(title: str, disposal: str = "") -> str:
    """Why a decision is not worth reading ('' when it is): by its case type and disposal."""
    t = case_type(title)
    if t and HC_MISC_TYPES.fullmatch(t):
        return "bail/interim/misc"
    if HC_NO_DECISION.search(disposal or ""):
        return "no decision"
    if t and HC_OFF_TOPIC_TYPES.fullmatch(t) and not HC_STRONG_PARTIES.search(title or ""):
        return "off-topic type"
    return ""


def hc_candidates(rows: list[dict], key: str, since: date, mark: dict | None = None, skipped: dict | None = None) -> list[HCCandidate]:
    """Rows of one bench's metadata file worth reading, oldest first: decided since `since`, not read yet by the bench's
    watermark (mark: {"date": iso, "pdfs": {basename: iso date} read in the HC_GRACE days before it}), and not ruled
    out by hc_skip_reason."""
    m = re.search(r"year=(\d+)/court=([^/]+)/bench=([^/]+)/", key)
    if not m:
        return []
    year, court_dir, bench = int(m.group(1)), m.group(2), m.group(3)
    mark = mark or {}
    mark_date = _date(mark.get("date"))
    floor = mark_date - timedelta(days=HC_GRACE) if mark_date else None
    mark_pdfs = set(mark.get("pdfs") or [])
    out = []
    for r in rows:
        when = _date(r.get("decision_date"))
        link = os.path.basename(str(r.get("pdf_link") or ""))
        if not when or when < since or not link or link == "None":
            continue
        if floor and (when < floor or link in mark_pdfs):
            continue
        title = _clean(str(r.get("title") or ""))
        why = hc_skip_reason(title, str(r.get("disposal_nature") or ""))
        if why:
            if skipped is not None:
                skipped[why] = skipped.get(why, 0) + 1
            continue
        out.append(HCCandidate(key, year, court_dir, bench, title, _clean(str(r.get("court") or "")) or "High Court", when, link,
                               strong=bool(HC_STRONG_PARTIES.search(title))))
    return sorted(out, key=lambda c: c.order)


def _hc_clean_lines(text: str) -> str:
    """The judgment's text without the party, counsel and address lines (header) or address lines (body)."""
    m = _HC_BODY.search(text or "", 100)
    cut = m.start() if m else min(len(text or ""), 1500)
    head = [ln for ln in text[:cut].splitlines() if not _PARTY_LINE.search(ln)]
    body = [ln for ln in text[cut:].splitlines() if not _ADDRESS_LINE.search(ln)]
    return "\n".join(head + body)


def hc_text_match(text: str, strong: str = "") -> str:
    """Why a judgment's opening pages make it ours ('' when they do not): an animal statute cited, or the animal terms
    at least twice outside the party and address lines; or `strong` (an animal-welfare body as a party) with readable
    text. Bail orders filed under another case type are not."""
    clean = _PLACE_NAME.sub(" ", _hc_clean_lines(text or ""))
    if _BAIL_TEXT.search(clean[:1500]):  # the opening says what is sought; later pages may mention earlier bail
        return ""
    m = ANIMAL_STATUTES.search(clean)
    if m:
        return "cites " + re.sub(r"\s+", " ", m.group(0)).strip()[:80]
    hits = [h.lower() for h in ANIMAL_TERMS.findall(DEPARTMENT_ONLY.sub(" ", clean))]
    if len(hits) >= 2:
        return "terms: " + ", ".join(sorted(set(hits)))[:80] + f" ({len(hits)} hits)"
    if strong and len(clean.strip()) >= 100:
        return "party: " + strong
    return ""


def hc_excerpt(text: str, limit: int = 4000) -> str:
    """Plain text for the summary and the relevance check: the opening (court, parties) and the passage around the
    first animal statute or term."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    m = ANIMAL_STATUTES.search(flat) or ANIMAL_TERMS.search(flat)
    if not m or m.start() < limit - 600:
        return flat[:limit]
    start = max(700, m.start() - 800)
    return flat[:600] + " … " + flat[start: start + limit - 700]


def hc_item(source: Source, c: HCCandidate, text: str, why: str) -> Item:
    when = c.when.strftime("%-d %b %Y")
    body = f"Judgment of the {c.court}, {when}. Case: {c.title}. Text (opening pages): {hc_excerpt(text)}"
    return Item(source, f"{c.title} ({c.court}, {when})", c.url, c.when, body[:4500],
                f"{c.court} · Indian High Court Judgments dataset (CC BY 4.0)",
                doc_kind="judgment", court="high-court", document_url=c.url)


def _fetch_pdf(url: str) -> bytes | None:
    """The PDF; None when the dataset has none (404). Other failures raise: the PDF is tried again next run."""
    try:
        return _get(url).content
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (403, 404):  # S3 answers 403 for a missing key
            return None
        raise


def add_hc_text(cands: list[HCCandidate], fetch=_fetch_pdf, deadline: float | None = None, stats: dict | None = None):
    """Read each candidate's first pages, in order; yields (candidate, text, why) for every one read (why '' when not
    kept; text None when the download failed, to be tried again). Stops at the deadline (time.monotonic()); what was
    not read is read next run."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    stats = stats if stats is not None else {}

    def read(c: HCCandidate):
        try:
            content = fetch(c.url)
        except Exception:  # noqa: BLE001  a timeout or 5xx: next run
            return c, None
        if content is None:
            return c, ""
        return c, pdf_text(content, max_pages=HC_PAGES) or " "

    with ThreadPoolExecutor(HC_THREADS) as pool:
        for i in range(0, len(cands), HC_THREADS * 4):  # in batches, so a deadline stops between two
            if deadline is not None and time.monotonic() > deadline:
                stats["stopped"] = True
                return
            for c, text in pool.map(read, cands[i: i + HC_THREADS * 4]):
                if text is None:
                    stats["failed"] = stats.get("failed", 0) + 1
                    yield c, None, ""
                    continue
                stats["read"] = stats.get("read", 0) + 1
                if text == "":
                    stats["missing"] = stats.get("missing", 0) + 1
                    yield c, "", ""
                    continue
                if len(text.strip()) < 100:
                    stats["no text"] = stats.get("no text", 0) + 1  # scanned: nothing to read
                strong = HC_STRONG_PARTIES.search(c.title) if c.strong else None
                yield c, text, hc_text_match(text, strong.group(0) if strong else "")


_s3 = None


def read_hc_rows(key: str, get=_get) -> list[dict]:
    """A bench's metadata, only the columns used (S3 range reads through pyarrow; the whole file if that fails)."""
    try:
        import pyarrow.fs as pafs
        import pyarrow.parquet as pq

        global _s3
        if _s3 is None:
            _s3 = pafs.S3FileSystem(anonymous=True, region="ap-south-1")
        f = pq.ParquetFile(_s3.open_input_file(f"indian-high-court-judgments/{key}"))
        return f.read(columns=[c for c in HC_COLUMNS if c in f.schema_arrow.names]).to_pylist()
    except Exception:  # noqa: BLE001  (no S3 support in this pyarrow, a network error...): the plain download
        return read_parquet(get(f"{HC_BUCKET}/{key}").content, HC_COLUMNS)


def _kept_item(source: Source, d: dict) -> Item:
    return Item(source, d["title"], d["url"], date.fromisoformat(d["date"]), d.get("text", ""), d.get("publisher", ""),
                doc_kind="judgment", court="high-court", document_url=d["url"])


def parse_judgments_hc(source: Source, body: str = "", get=_get, today: date | None = None, now: datetime | None = None,
                       rows_for=None, fetch=_fetch_pdf, max_pdfs: int | None = None, report: dict | None = None,
                       newest_first: bool = False) -> list[Item]:
    """High Court judgments about animals. Every run reads the metadata files that changed (or still have rows not
    read), then at most `max_pdfs` PDFs, oldest first. Kept judgments are remembered in the state until they leave the
    lookback window, so one found in a run that stopped early is still sent next run (the seen list skips sent ones).
    `report` (dry run) collects counts and the kept list; `newest_first` (dry run only) reads the newest PDFs first."""
    import time

    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    if not source.lookback_days:  # collect() applies it too: without it the run's default (14 days) would drop kept ones
        source.lookback_days = HC_LOOKBACK
    lookback = int(source.lookback_days)
    since = today - timedelta(days=lookback)
    rows_for = rows_for or (lambda key: read_hc_rows(key, get=get))
    cap = HC_MAX_PDFS if max_pdfs is None else max_pdfs
    st = state()
    st.pop("hc_checked", None)
    st.pop("hc_etags", None)  # before 25 Oct 2026: whole files, once a day
    hc = st.setdefault("hc", {})
    benches = hc.setdefault("benches", {})
    kept = [d for d in hc.get("kept", []) if (_date(d.get("date")) or since) >= since]
    report = report if report is not None else {}
    skipped: dict = report.setdefault("skipped", {})

    from concurrent.futures import ThreadPoolExecutor

    todo = []
    for year in sorted({today.year, since.year}):
        for obj in s3_list(HC_BUCKET, f"metadata/parquet/year={year}/", get=get):
            key = obj["key"]
            if not key.endswith("/metadata.parquet"):  # metadata-mobile.parquet: an older copy, not updated since June
                continue
            b = benches.get(key, {})
            if b.get("etag") == obj["etag"] and b.get("done"):
                continue
            todo.append(obj)

    def load(obj):
        try:
            return obj, rows_for(obj["key"]), None
        except Exception as exc:  # noqa: BLE001  one unreadable file must not stop the others
            return obj, None, exc

    cands: list[HCCandidate] = []
    files: dict[str, dict] = {}
    with ThreadPoolExecutor(HC_THREADS) as pool:
        for obj, rows, exc in pool.map(load, todo):
            key = obj["key"]
            if rows is None:
                report.setdefault("errors", []).append(f"{key}: {exc}")
                continue
            b = benches.get(key, {})
            report["rows"] = report.get("rows", 0) + len(rows)
            found = hc_candidates(rows, key, since, b.get("mark"), skipped)
            files[key] = {"etag": obj["etag"], "total": len(found)}
            cands += found
            if not found:
                benches[key] = {**b, "etag": obj["etag"], "done": True}
    report["candidates"] = len(cands)
    # Oldest first, so the PDFs read from each bench are the start of its list (the watermark).
    cands.sort(key=lambda c: c.order, reverse=newest_first)
    deadline = time.monotonic() + HC_MINUTES * 60 if not newest_first else None
    read: dict[str, list[HCCandidate]] = {}
    for c, text, why in add_hc_text(cands[:cap], fetch=fetch, deadline=deadline, stats=report):
        if text is None:  # download failed: not marked read
            continue
        read.setdefault(c.key, []).append(c)
        if why:
            it = hc_item(source, c, text, why)
            kept = [d for d in kept if d["url"] != it.url]
            kept.append({"title": it.title, "url": it.url, "date": it.date.isoformat(), "text": it.text,
                         "publisher": it.publisher, "why": why})
            report.setdefault("kept", []).append({"title": c.title, "court": c.court, "date": c.when.isoformat(), "why": why, "pdf": c.url})
    if not newest_first:
        for key, info in files.items():
            done = read.get(key, [])
            if not done:
                continue
            b = benches.get(key, {})
            mark = b.get("mark") or {}
            last = max([c.when for c in done] + ([_date(mark["date"])] if mark.get("date") else []))
            floor = (last - timedelta(days=HC_GRACE)).isoformat()
            pdfs = {**(mark.get("pdfs") or {}), **{c.pdf: c.when.isoformat() for c in done}}
            pdfs = {k: v for k, v in sorted(pdfs.items()) if v >= floor}
            benches[key] = {"etag": info["etag"], "done": len(done) >= info["total"], "mark": {"date": last.isoformat(), "pdfs": pdfs}}
    hc["kept"] = kept
    return sorted((_kept_item(source, d) for d in kept), key=lambda i: i.date, reverse=True)


# ---------- Gazette of India ----------

def gazette_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for sub in soup.select("[id^=gvGazetteList_lbl_Subject_]"):
        n = sub["id"].rsplit("_", 1)[-1]
        cell = lambda name: _clean((soup.find(id=f"gvGazetteList_lbl_{name}_{n}") or {}).get_text(" ") if soup.find(id=f"gvGazetteList_lbl_{name}_{n}") else "")
        rows.append({"ministry": cell("Ministry"), "department": cell("Department"), "subject": _clean(sub.get_text(" ")),
                     "part": cell("PartSection"), "published": cell("PublishDate"), "ugid": cell("UGID")})
    return rows


def gazette_items(source: Source, rows: list[dict], ministries: list[str]) -> list[Item]:
    items = []
    for r in rows:
        # Weekly gazettes list "This Gazette may contains Multiple Ministry / Organization" (half the recent uploads,
        # 12 Oct 2026): those are judged on the subject alone.
        multiple = r["ministry"].lower().startswith("this gazette may contain")
        if not multiple and not any(m.lower() == r["ministry"].lower() for m in ministries):
            continue
        if not about_animals(r["subject"]):
            continue
        when = _date(r["published"])
        num = r["ugid"].rsplit("-", 1)[-1]
        if not when or not num.isdigit():
            continue
        pdf = f"https://egazette.gov.in/WriteReadData/{when.year}/{num}.pdf"
        text = f"Gazette of India, {r['part']}. {r['ministry']}" + (f", {r['department']}" if r["department"] not in ("", "Not Applicable") else "") + f". Gazette ID {r['ugid']}. Subject: {r['subject']}."
        items.append(Item(source, r["subject"], pdf, when, text, "Gazette of India", doc_kind="notification", document_url=pdf))
    return items


_NUMBER = re.compile(r"\b(G\.\s?S\.\s?R\.|S\.\s?O\.)\s*(\d+)\s*\(E\)\s*\.?\s*[\u2014\u2013-]")
_DRAFT = re.compile(r"\bdraft\s+(?:rules|regulations|notification|amendment|scheme|order)\b|objections\s+(?:and|or)\s+suggestions[^.]{0,200}(?:may be|shall be|will be|within)", re.I)
_FINAL = re.compile(r"hereby\s+makes\s+the\s+following|(?:have|has|had)\s+been\s+(?:duly\s+)?considered|were\s+considered", re.I)


def notification_details(text: str) -> tuple[str, bool, str]:
    """(number, draft?, English opening) from a Gazette notification's text. The English part starts at the number
    ("G.S.R. 864(E).—In exercise of ..."); the Hindi part before it is skipped. A draft invites objections and
    suggestions; final rules say the objections "have been considered" and that the Government "hereby makes" them."""
    m = _NUMBER.search(text)
    if not m:
        return "", False, ""
    number = f"{'G.S.R.' if m.group(1).startswith('G') else 'S.O.'} {m.group(2)}(E)"
    opening = re.sub(r"\s+", " ", text[m.start(): m.start() + 3000]).strip()
    draft = bool(_DRAFT.search(opening)) and not _FINAL.search(opening)
    return number, draft, opening


def pdf_text(content: bytes, max_pages: int = 120) -> str:
    """Text of a PDF (pypdf); '' when it cannot be read."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages[:max_pages])
    except Exception:  # a broken or scanned PDF: the listing is all we have
        return ""


def add_gazette_details(item: Item, session: requests.Session) -> Item:
    """Number, draft flag and the English opening from the notification itself (the listing says neither)."""
    try:
        resp = session.get(item.url, timeout=TIMEOUT * 4)
        resp.raise_for_status()
    except requests.RequestException:
        return item
    number, draft, opening = notification_details(pdf_text(resp.content))
    item.legal_number = number
    item.draft = draft
    if number:
        item.text += f" Notification {number}."
    if draft:
        if "draft" not in item.title.lower():
            item.title = f"Draft {item.title}"
        item.text += (" This is a DRAFT published for objections and suggestions, not rules in force: say it is proposed"
                      " and open for objections; never say the rules were issued, notified or made.")
    if opening:
        item.text += f" Text: {opening[:1500]}"
    return item


def _ca_bundle() -> str:
    """egazette.gov.in sends no intermediate (Let's Encrypt YR1 since Oct 2026, YR2 before): see sources.ca_bundle."""
    from .sources import ca_bundle

    return ca_bundle()


def parse_egazette(source: Source, body: str = "", session: requests.Session | None = None, pages: int = 5) -> list[Item]:
    s = session or requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if session is None:
        s.verify = _ca_bundle()
    try:
        home = s.get("https://egazette.gov.in/", timeout=TIMEOUT * 2)
    except (requests.ConnectionError, requests.Timeout):
        from . import relay

        if session is not None or not relay.allowed("https://egazette.gov.in/"):
            raise
        # Unreachable from GitHub: the first page of each list and the notifications through the site (its pages
        # after the first need the form posts the relay does not make).
        s, pages = relay.Session(), 1
        home = s.get("https://egazette.gov.in/", timeout=TIMEOUT * 2)
    home.raise_for_status()
    base = home.url.rsplit("/", 1)[0]
    rows = []
    for category in (6, 7):  # Extraordinary, Weekly
        url = f"{base}/RecentUploads.aspx?Category={category}"
        resp = s.get(url, headers={"Referer": home.url}, timeout=TIMEOUT * 2)
        resp.raise_for_status()
        rows += gazette_rows(resp.text)
        for page in range(2, pages + 1):
            soup = BeautifulSoup(resp.text, "html.parser")
            form = {i["name"]: i.get("value", "") for i in soup.select("input[type=hidden][name]")}
            if f"Page${page}" not in resp.text:
                break
            form.update({"__EVENTTARGET": "gvGazetteList", "__EVENTARGUMENT": f"Page${page}"})
            resp = s.post(url, data=form, headers={"Referer": url}, timeout=TIMEOUT * 2)
            resp.raise_for_status()
            rows += gazette_rows(resp.text)
    return [add_gazette_details(i, s) for i in gazette_items(source, rows, source.ministries)]
