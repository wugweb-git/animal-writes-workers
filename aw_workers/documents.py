"""Library documents: Gazette of India notifications and court judgments, as documents (not news about them).

- egazette:     egazette.gov.in "Recent uploads" (latest 100 Extraordinary and Weekly gazettes, no CAPTCHA). Kept when the
                ministry is one we follow and the subject is about animals. PDF: egazette.gov.in/WriteReadData/<year>/<id>.pdf
- judgments-sc: Supreme Court judgments from the open dataset s3://indian-supreme-court-judgments (CC BY 4.0,
                github.com/vanga/indian-supreme-court-judgments): one metadata file per year.
- judgments-hc: High Court judgments from s3://indian-high-court-judgments (CC BY 4.0): one metadata file per court bench
                and year. Checked at most once a day; a file is read again only when it has changed.

Government Acts, rules, notifications and judgments may be reproduced (s.52(1)(q), Copyright Act 1957); the site keeps a
copy of the PDF and credits the source. Interim orders and bail matters are skipped: the Library keeps decisions.
"""

from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
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
SKIP_MATTERS = re.compile(r"\bbail\b|anticipatory|\binterim\b|\b(?:ABA|CRMBA|CRLMBA|BA)\b|\bB\.\s?A\.", re.I)

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

def hc_items(source: Source, rows: list[dict], key: str, year: int) -> list[Item]:
    m = re.search(r"court=([^/]+)/bench=([^/]+)/", key)
    if not m:
        return []
    court_dir, bench = m.groups()
    items = []
    for r in rows:
        title = _clean(str(r.get("title") or ""))
        if str(r.get("is_final", "True")) == "False" or SKIP_MATTERS.search(_text(title, r.get("order_type"), r.get("case_type"), r.get("purpose_name"))):
            continue
        # Case title and the judgment's opening text only: the page markup around each row repeats unrelated words.
        if not about_animals(_text(title, r.get("description"))):
            continue
        when = _date(r.get("decision_date"))
        link = os.path.basename(str(r.get("pdf_link") or ""))
        if not when or not link or link == "None":
            continue
        pdf = f"{HC_BUCKET}/data/pdf/year={year}/court={court_dir}/bench={bench}/{link}"
        court = _clean(str(r.get("court") or "High Court"))
        items.append(Item(source, f"{title} ({court}, {when.strftime('%-d %b %Y')})", pdf, when,
                          _clean(_text(r.get("description")))[:4000], f"{court} · Indian High Court Judgments dataset (CC BY 4.0)",
                          doc_kind="judgment", court="high-court", document_url=pdf))
    return items


def parse_judgments_hc(source: Source, body: str = "", get=_get, today: date | None = None, now: datetime | None = None) -> list[Item]:
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    st = state()
    last = st.get("hc_checked")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=20):
        return []  # once a day is enough; the dataset itself updates daily
    etags = st.setdefault("hc_etags", {})
    items = []
    for obj in s3_list(HC_BUCKET, f"metadata/parquet/year={today.year}/", get=get):
        key = obj["key"]
        if not key.endswith(".parquet") or etags.get(key) == obj["etag"]:
            continue
        rows = read_parquet(get(f"{HC_BUCKET}/{key}").content,
                            ["title", "description", "decision_date", "pdf_link", "court", "is_final", "order_type", "case_type", "purpose_name"])
        items += hc_items(source, rows, key, today.year)
        etags[key] = obj["etag"]
    st["hc_checked"] = now.isoformat()
    return sorted(items, key=lambda i: i.date, reverse=True)


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
