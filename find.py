"""UK public tenders for software testing / QA / performance / data masking.

One module, two official OCDS feeds, one sieve:

  * Find a Tender      — the large UK register (threshold and above)
      https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages  (updatedFrom/updatedTo)
  * Contracts Finder   — the smaller notices below that threshold
      https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search  (publishedFrom/publishedTo)

Both answer OCDS JSON (releases[] + links.next) and are read straight through `fetch` — no
browser, no key, no AI. Each is asked one day and one procurement stage at a time, so a run
reads the open opportunities instead of the whole window's traffic, and every day it does
read is kept even if the next one fails. The sieve is pure rules (CPV code + wording, with vetoes), so a run is
deterministic and costs nothing.

THE SIEVE (unchanged from the pond engine):
  Tier 1 — main CPV 72254 (software testing), OR an IT/software code with a strong
           software-testing phrase in the text, OR an IT code carrying CPV 72254 with QA wording.
  Tier 2 — an IT/software code with quality-assurance / testing wording.
  Tier 3 — a software-testing phrase in a NON-IT procurement (widest match; off by default).
  Gated on the MAIN CPV (tender.classification), never a parasitic add-on code, with vetoes on
  construction, clinical/lab, security-only, licences, learning platforms, etc.

A bid carries the sentence that says what must be done (summary), the full notice (description),
and the rule that took it with its evidence quote (matched). Tommo Bids triages it from there.
"""
import re
import threading
import time
from tommos_module import ContractError, a_bid, a_date, a_key, fetch, log, the_cursor

# ----------------------------------------------------------------------------- knobs
INCLUDE_TIER_3 = True       # tier 3 = widest match; emitted, Tommo Bids triages it downstream
FIRST_RUN_DAYS = 7          # how far back the very first run looks (no cursor yet)
# A held cursor is the right answer to one failed read, but not to twenty: the window keeps
# widening, the page keeps growing (a 12-day window of Find a Tender is 1.6 MB and 28 s on a
# good minute), and a feed that is merely slow becomes a feed that can never be read again.
# So the look-back is capped. When the cap bites, the run says plainly which span it skipped.
MAX_LOOKBACK_DAYS = 30     # wide enough to read itself out of a backlog; the clamp still
                           # says out loud which days it refuses to read
MAX_PAGES = 12              # page cap per feed, so one run stays inside the time limit

# Both feeds answer every notice updated in the window -- awards, amendments, contract
# records, the lot -- and the sieve throws away everything that is not an open opportunity.
# A day of Find a Tender is 4 to 10 pages of 100 (the pond's own daily log, 12-20 November).
# A feed takes ONE stage per call: a repeated or comma-joined `stages` keeps only the last.
# Contracts Finder ONLY. Its `stages` filter is exact: measured on 10 and 19 November 2026,
# the tender-tagged releases of a day read unfiltered and the releases the filter returns are
# the same set, nothing lost, nothing extra -- and it saves reading ~110 releases a day to keep
# ~12. Find a Tender's filter of the same name is NOT used; see FTS_NO_STAGES below.
CF_STAGES = ("tender", "planning")

# Find a Tender is read WITHOUT `stages`, and this is not a preference -- the filter is broken.
# Measured 20 November 2026 on the day 10 November: the feed holds 407 notices that day, 83 of
# them open (tender.status = active). `stages=tender` returned 8, and every one of the 8 carries
# legalBasis 32014L0024 -- the old EU directive. All 71 open notices published under the
# Procurement Act 2023 (legalBasis 2023/54) were missing, among them the one that gave this
# away: "Electronic Committee Management System (ECMS)", Caerphilly, closing 11 December, which
# the pond (which never filtered) had and the module never saw. Two independent developer
# write-ups report the same fault. Since the Act commenced, 2023/54 is the overwhelming majority
# of the feed -- 348 of those 407 -- so the filter hides most of it. The whole window is read and
# the sieve decides, which is what the pond always did.
FTS_NO_STAGES = True

# Where the re-reading of Find a Tender starts, once, under the new cursor key. The module's
# first ever run was 12 November 2026; everything it read up to 17 November it read with the
# broken versions, and everything after that through the broken filter. This date is a day
# before all of it, so the notices those runs lost come in by themselves -- ECMS among them.
# A later version, once the catch-up is done, can drop this to a plain "a week back".
FTS_READ_FROM = "2026-11-10T00:00:00Z"

PAGE_LIMIT = 100            # the most either feed gives on one page (500 answers HTTP 400)

# Tommos allows a run up to 200 s, and this module asks for all of it (Yuri, 20.11.2026:
# "we are not in a hurry -- one or two runs a day can wait for the host to answer"). The
# portal is the slow party: 17-31 s for a good page, and on a bad day it does not answer
# inside 90 s at all. Waiting is cheaper than losing a day of notices. Find a Tender can be slow (17-31 s a page, and on a bad day
# it does not answer at all), and the SDK's own retrying would spend 90 s on a single page
# before giving up -- which is exactly how the 17 Nov 08:00 run died with "Out of time"
# and nothing read. So: one try per page, a timeout that never outlives the budget, and a
# wall clock that stops the paging in time for the other feed to be read at all. A window
# cut short this way is INCOMPLETE -- its cursor stays put and it is read again next run.
RUN_BUDGET_S = 175          # wall clock the whole run may spend fetching
FTS_SHARE = 0.85            # of it, at most this much goes to Find a Tender
PAGE_TIMEOUT_S = 90         # longest wait for one page
# One attempt per page, on purpose. The Find a Tender team asked us not to retry straight after a
# timeout but to wait a few minutes (Philip Wong, ticket 0037632, 19.11.2026), and a run that must
# end inside 120 s has no few minutes to wait. So a page that fails is simply not read now: the
# day-by-day cursor leaves that window open and the next run reads it. The tools already space
# calls to one host by 500 ms, which is the other half of what they asked for.
MIN_PAGE_S = 10             # do not start a page with less time left than this

# Contracts Finder answers in under a second, so it needs almost none of the budget;
# Find a Tender is the slow one (28 s for a wide window on 17 Nov, and on a bad minute
# it does not answer at all), so it gets the rest. A page that did not answer is tried
# again rather than abandoned: the portal's silences are short and the run has time.

FTS_API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_API = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
NOTICE_URL = {
    "find-a-tender": "https://www.find-tender.service.gov.uk/procurement/{ocid}",
    "contracts-finder": "https://www.contractsfinder.service.gov.uk/Notice/{guid}",
}

# ----------------------------------------------------------------------------- the sieve (ported)
CPV_GOLD = ("72254",)                 # software / system testing
CPV_IT = ("72",)                      # IT services, programming, consultancy, support
CPV_SW = ("48",)                      # software packages
CPV_VETO = ("85", "71", "45", "50", "90", "03", "15", "44", "42", "31", "34", "60", "55",
            "09", "14", "16", "33", "35", "39", "43", "75", "77", "98", "92", "80",
            "7960", "7961", "7962", "7221218")

STRONG = re.compile(
    r"\b(load[\s-]?test\w*|performance test\w*|soak test\w*|scalability test\w*|"
    r"non[\s-]?functional test\w*|functional test\w*|software test\w*|application test\w*|"
    r"system test\w*|integration test\w*|smoke test\w*|continuous test\w*|"
    r"user acceptance test\w*|\buat\b|test automation|automated test\w*|qa automation|"
    r"test management|test strateg\w*|test data manage\w*|data mask\w*|data obfuscat\w*|"
    r"synthetic (test )?data|quality engineering|sdet|regression test\w*|"
    r"end[\s-]?to[\s-]?end test\w*|quality assurance and testing|testing for (the )?it)\b", re.I)
STRONG_SW = re.compile(
    r"\b(software test\w*|application test\w*|system test\w*|integration test\w*|"
    r"continuous test\w*|functional test\w*|non[\s-]?functional test\w*|regression test\w*|"
    r"user acceptance test\w*|\buat\b|test automation|automated test\w*|qa automation|"
    r"test management|test strateg\w*|test data manage\w*|data mask\w*|data obfuscat\w*|"
    r"synthetic (test )?data|quality engineering|sdet|end[\s-]?to[\s-]?end test\w*|"
    r"quality assurance and testing|testing for (the )?it)\b", re.I)
SEC = re.compile(
    r"\b(penetration test\w*|pen[\s-]?test\w*|vulnerability (assessment|management|scan\w*)|"
    r"cyber[\s-]?security|ethical hack\w*|red team\w*|\bCHECK\b|\bCREST\b|"
    r"security operations|\bSOC\b|threat (intelligence|hunting))\b", re.I)
LAB = re.compile(
    r"\b(sequencing|\bngs\b|genomic|bacteriophage|coagulation|\bassay\b|reagent|histolog\w*|"
    r"cytolog\w*|microbiolog\w*|pathology|radioactive|calibration|analyser|analyzer|"
    r"chromatograph\w*|spectromet\w*|clinical trial|in vitro|specimen|biomarker|"
    r"diagnos\w*)\b", re.I)
PHYS = re.compile(
    r"\b(tunnel|bridge|carriageway|highway|pavement|footway|cabling|overhead line|"
    r"wood pole|underground (cable|asset)|drainage|sewer|culvert|water main|pipework|"
    r"roofing|cladding|hvac|boiler|chiller|escalator|street lighting|grounds maintenance|"
    r"landscaping|refuse|waste (collection|management)|in cab|gritting|winter maintenance|"
    r"building works|construction works|demolition|fit[\s-]?out|catering|cleaning services|"
    r"security guarding|fencing|vehicle telematics|journey recorder|\bohle\b|overhead line|"
    r"rolling stock|locomotive|\btram\b|liquid handling|brake|test rig|test stand|"
    r"wind tunnel|turbine blade|erosion|camera upgrade)\b", re.I)
LIC = re.compile(r"\b(licen[cs]e[sd]?|licensing|subscription[s]?|renewal of|"
                 r"licen[cs]e renewal|maintenance and support)\b", re.I)
LMS = re.compile(r"\b(learning management system|\blms\b|e-?portfolio|e-?learning|"
                 r"virtual learning environment|\bvle\b|learner management)\b", re.I)
EMAILRE = re.compile(r"^\s*(fwd|fw|re)\s*:", re.I)
TRANSP = re.compile(r"\b(transparency notice|voluntary ex[\s-]?ante|\bveat\b)\b", re.I)
PLATFORMS = ("bip solutions", "delta esourcing", "due north", "proactis", "procontract",
             "in-tend", "intend ", "atamis", "jaggaer", "sell2wales", "public contracts scotland")
FX_TO_GBP = {"GBP": 1.0, "EUR": 0.85, "USD": 0.79}

_SOFT = re.compile(r"(software|application|platform|\bsystem\b|digital|\bdata\b|\bIT\b|cloud|"
                   r"website|web\b|api\b|database)", re.I)
_QA_HARD = re.compile(r"(software quality|testing services|test services|"
                      r"independent test\w*|verification and validation|iv&v|acceptance test\w*|"
                      r"accessibility test\w*|usability test\w*|quality engineering)", re.I)
_TEST_TOKEN = re.compile(r"\btest(?:ing|ed|s)?\b", re.I)
_EXAM = re.compile(r"\b(theory test|driving test|driver test|eye test|sight test|hearing test|"
                   r"blood test|drug test|dna test|dbs|antigen|pcr|specimen|pathology|"
                   r"water test\w*|soil test\w*|material test\w*|lamp test|emissions test\w*|"
                   r"pat test\w*|electrical test\w*|pressure test\w*)\b", re.I)
_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+|\s•\s|\s[-–]\s(?=[A-Z])")
TITLE_TESTING = re.compile(r"\b(test(s|ing|er|ers)?|qa|quality assurance|"
                           r"verification|validation|v&v|iv&v)\b", re.I)

SCOPE_CUE = re.compile(
    r"invit\w+\s+tender|is seeking|seeks?\b|requires?\b|provision of|to provide|"
    r"supply (of|and)|procurement of|contract (is )?for|wish\w*\s+to\s+"
    r"(appoint|procure|engage|commission)|appoint\w*\s+a\b|looking for|"
    r"deliver\w*|scope of|intend\w*\s+to|is for the", re.I)
SCOPE_OF = re.compile(r"(provision|supply|delivery|procurement|implementation|"
                      r"maintenance|installation|design|development)\s+(and\s+\w+\s+)*of\b", re.I)
SCOPE_STRONG = re.compile(r"invit\w+\s+tender|is seeking|requires? a\b|wish\w*\s+to|"
                          r"is for the (provision|supply|delivery|procurement)", re.I)
SCOPE_BOILER = re.compile(
    r"please read|register|express\w*\s+(an\s+)?interest|important:|log\s?in|"
    r"this (event|opportunity|notice)|closing date|deadline for|clarification|"
    r"questions? (should|must|are)|e-?tender|e-?sourcing|\bportal\b|website|"
    r"in order to (view|access|participate|express)|bravo|jaggaer|proactis|delta "
    r"esourcing|mytenders|contracts finder|find a tender|before deciding", re.I)


def fix_mojibake(s):
    if s and any(m in s for m in ("Ã", "â", "Â")):
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return s
    return s


def best_quote(title, desc):
    """The one verbatim sentence that most clearly shows the tender is ours.
    Returns (quote, strong, qa)."""
    cands = [p.strip(' \t"“”') for p in _SPLIT.split(desc or "") if p and p.strip()]
    if title:
        cands.append(title.strip())
    cands = [c for c in cands if 12 <= len(c) <= 320]
    best, best_score, best_strong, best_qa = None, 0, False, False
    for c in cands:
        strong = bool(STRONG.search(c))
        qa_hard = bool(_QA_HARD.search(c))
        test_tok = bool(_TEST_TOKEN.search(c))
        if not (strong or qa_hard or test_tok):
            continue
        if _EXAM.search(c) and not strong:
            continue
        score = (12 * len(STRONG.findall(c))
                 + 6 * len(set(m.lower() for m in _QA_HARD.findall(c)))
                 + (2 if test_tok else 0)
                 + 2 * len(set(m.lower() for m in _SOFT.findall(c))))
        if strong:
            score += 6
        qa_soft = qa_hard and (bool(_SOFT.search(c)) or bool(re.search(
            r"software quality|testing services|test services|quality engineering|iv&v|"
            r"verification and validation", c, re.I)))
        if score > best_score:
            best, best_score, best_strong, best_qa = c, score, strong, (strong or qa_soft)
    if not best:
        return None, False, False
    q = re.sub(r"\s+", " ", best).strip()
    if len(q) > 240:
        q = q[:240].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return q, best_strong, best_qa


def scope_sentence(title, desc):
    """The sentence that best answers 'what must be done' — the kind of work."""
    text = fix_mojibake(desc or "").replace("\n", " ")
    sents = re.split(r"(?<=[.!?])\s+", text)
    best, best_score = None, 0.0
    for i, s in enumerate(sents[:12]):
        s = s.strip().lstrip("•-–—*· ").strip()
        if len(s) < 25:
            continue
        sc = 0.0
        if SCOPE_CUE.search(s):    sc += 2
        if SCOPE_OF.search(s):     sc += 2
        if SCOPE_STRONG.search(s): sc += 2
        if SCOPE_BOILER.search(s): sc -= 6
        sc -= i * 0.4
        if sc > best_score:
            best_score, best = sc, s
    if not best:
        best = next((s.strip() for s in sents if len(s.strip()) >= 25), title)
    if best and len(best) > 240:
        best = best[:240].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return best or title


def main_cpv(t):
    c = t.get("classification") or {}
    if c.get("id"):
        return str(c["id"])
    for it in (t.get("items") or []):
        ic = it.get("classification") or {}
        if ic.get("id"):
            return str(ic["id"])
    return ""


def all_cpvs(t):
    out = []

    def add(c):
        if c and c.get("id"):
            out.append(str(c["id"]))
    add(t.get("classification"))
    for c in (t.get("additionalClassifications") or []):
        add(c)
    for it in (t.get("items") or []):
        add(it.get("classification"))
        for c in (it.get("additionalClassifications") or []):
            add(c)
    return out


def pref(code, prefs):
    return any(code.startswith(p) for p in prefs)


def stage_of(r, t):
    awards = r.get("awards") or []
    status = (t.get("status") or "").lower()
    if awards:
        return "awarded"
    if status in ("planning", "planned"):
        return "planning"
    if status == "active":
        return "open"
    if status in ("complete", "unsuccessful", "cancelled", "withdrawn"):
        return status
    return status or "unknown"


def is_framework(title, desc, amt_gbp):
    if re.search(r"\b(framework|dynamic purchasing|\bDPS\b|call[\s-]?off)\b", title + " " + desc, re.I):
        return True
    if re.search(r"\bRM\d{3,4}\b", title):
        return True
    return bool(amt_gbp and amt_gbp > 50_000_000)


def resolve_buyer(r):
    raw = fix_mojibake((r.get("buyer") or {}).get("name") or "")
    plat = lambda n: any(p in (n or "").lower() for p in PLATFORMS)
    cands = [fix_mojibake(p.get("name") or "") for p in (r.get("parties") or [])
             if "buyer" in (p.get("roles") or [])]
    cands = [c for c in cands if c and not plat(c)]
    if raw and not plat(raw):
        cands.append(raw)
    if cands:
        return max(cands, key=len)
    return raw or None


def scrub_deadline(d):
    if not d or re.match(r"^\d{4}-01-01T00:00:00", d):
        return None
    return d


def notice_url(source, ocid):
    if not ocid:
        return None
    guid = ocid.split("-", 2)[2] if ocid.count("-") >= 2 else ocid
    return NOTICE_URL[source].format(ocid=ocid, guid=guid)


def _cf_notice_url(rel):
    # Contracts Finder's public /Notice/ page uses a DIFFERENT guid from the one
    # inside the OCID: the real link sits in tender.documents, and equals the
    # release id (<uuid>-<serial>, strip the serial). The OCID-guid form 404s.
    t = rel.get("tender") or {}
    for doc in (t.get("documents") or []):
        u = doc.get("url") or ""
        if "/Notice/" in u and "/Attachment/" not in u:
            return u.split("?")[0]
    rid = rel.get("id") or ""
    m = re.match(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                 r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:-\d+)?$", rid)
    if m:
        return f"https://www.contractsfinder.service.gov.uk/Notice/{m.group(1)}"
    return None


def record_url(source, ocid, rel):
    if source == "contracts-finder":
        u = _cf_notice_url(rel)
        if u:
            return u
    return notice_url(source, ocid)


def classify(r, source):
    """Apply the sieve to one OCDS release. Returns an internal dict, or None if not ours."""
    t = r.get("tender") or {}
    title = fix_mojibake(t.get("title") or "")
    desc = fix_mojibake(t.get("description") or "")
    status = (t.get("status") or "").lower()
    deadline = scrub_deadline((t.get("tenderPeriod") or {}).get("endDate"))

    cs = all_cpvs(t)
    main = main_cpv(t)
    main_veto = bool(main) and pref(main, CPV_VETO)
    off = bool(PHYS.search(title) or SEC.search(title) or LAB.search(title)
               or LIC.search(title) or LMS.search(title)
               or EMAILRE.search(title) or TRANSP.search(title))
    has_gold = any(pref(c, CPV_GOLD) for c in cs)
    it_ctx = ((pref(main, CPV_IT) or pref(main, CPV_SW)
               or (not main and any(pref(c, CPV_IT) or pref(c, CPV_SW) for c in cs)))
              and not main_veto and not off)
    gold_main = pref(main, CPV_GOLD) and not off
    veto_all = bool(cs) and all(pref(c, CPV_VETO) for c in cs)

    quote, quote_strong, quote_qa = best_quote(title, desc)
    tier = None
    if gold_main:
        tier = 1
    elif it_ctx and quote_strong:
        tier = 1
    elif (not main_veto) and has_gold and quote_qa and not off:
        tier = 1
    elif it_ctx and quote_qa:
        tier = 2
    elif quote and STRONG_SW.search(quote) and not it_ctx and not main_veto and not veto_all and not off:
        tier = 3
    if not tier:
        return None

    val = t.get("value") or {}
    amt = val.get("amount")
    cur = val.get("currency")
    amt_gbp = round(amt * FX_TO_GBP.get(cur, 1.0)) if isinstance(amt, (int, float)) else None
    return {
        "tier": tier,
        "borderline": not gold_main and not TITLE_TESTING.search(title),
        "ocid": r.get("ocid") or "",
        "source_url": record_url(source, r.get("ocid") or "", r),
        "date": r.get("date"),
        "stage": stage_of(r, t),
        "buyer": resolve_buyer(r),
        "title": title,
        "cpv": sorted(set(cs))[:40],
        "amount": amt,
        "currency": cur if isinstance(cur, str) and re.match(r"^[A-Z]{3}$", cur or "") else None,
        "framework": is_framework(title, desc, amt_gbp),
        "deadline": deadline,
        "scope": scope_sentence(title, desc),
        "quote": quote,
        "description": desc,
    }


# ----------------------------------------------------------------------------- map to the Bid contract
_TIER_RULE = {
    1: "CPV 72254 software testing, or an IT/software procurement calling for software testing",
    2: "an IT/software procurement with quality-assurance / testing wording",
    3: "software-testing wording in a non-IT procurement (widest match)",
}


def _matched(row):
    rule = _TIER_RULE[row["tier"]]
    if row["borderline"]:
        rule += "; testing is a line item, not the main subject"
    if row.get("quote"):
        rule += ": «" + row["quote"] + "»"
    return rule if len(rule) <= 300 else rule[:299].rstrip() + "…"


def _opp_type(row):
    if row["framework"]:
        return "Framework / DPS (the value is a ceiling across suppliers, not one contract)"
    return row["stage"].capitalize()


def _to_bid(row, source):
    fields = dict(
        source=source,
        key=a_key(row["ocid"]),
        matched=_matched(row),
        title=row["title"] or "(untitled notice)",
        buyer={"name": row["buyer"] or "Unknown buyer"},
        source_url=row.get("source_url") or notice_url(source, row["ocid"]),
        country="GB",
        posted_date=row.get("date"),
        due_date=row.get("deadline"),
        summary=row.get("scope") or None,
        description=(row.get("description") or None),
        cpv=row.get("cpv") or None,
        opp_type=_opp_type(row),
    )
    if row.get("amount") is not None and row.get("currency"):
        fields["value_estimate"] = {"low": row["amount"], "high": row["amount"], "currency": row["currency"]}
    return a_bid(**fields)


# ----------------------------------------------------------------------------- the feeds
def _json(answer):
    # Some gov OCDS feeds carry raw control characters; parse leniently.
    import json
    return json.loads(answer.text(), strict=False)


def _fetch_bounded(url, seconds):
    """Fetch one page and give up after `seconds` of wall clock, whatever the socket is doing.

    urllib's timeout bounds ONE read on the socket, not the whole answer, so a 1.3 MB page
    that trickles in a few bytes at a time can outlive the run's budget -- and a run Tommos
    kills returns neither records nor cursor, which is how the 17 November 08:00 run lost a
    day. The fetch runs in a thread this function can abandon: the budget is kept whatever
    the portal does.
    """
    out = {}

    def go():
        try:
            out["answer"] = fetch(url, tries=1, timeout_s=max(1, int(seconds)))
        except Exception as e:      # noqa: BLE001 -- handed back to the caller below
            out["error"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds + 2)
    if t.is_alive():
        raise TimeoutError(f"the page did not arrive within {int(seconds)} s "
                           f"(abandoned so the run keeps its budget)")
    if "error" in out:
        raise out["error"]
    return out["answer"]


def _why_in_words(why):
    """A page that did not arrive, said so a reader knows whether we were refused or ignored."""
    t = (why or "").lower()
    if "429" in t:
        return ("the feed asked us to slow down (429); we stop rather than press it, and read "
                "no faster next run")
    if "answered 5" in t:
        return f"the feed is unwell and answered an error of its own ({why})"
    if "did not answer" in t:
        return ("the feed did not answer this page in time; it is not asked again now, because "
                "the service asks that a timeout be left alone for a few minutes")
    return f"this page could not be read ({why})"


def _pull(url, source, now_day, records, seen, deadline):
    """Page a feed from `url`, classify each release, append bids.

    Returns (scanned, on_theme, complete, unread). `complete` is False when the window was
    NOT read to its end -- the feed errored, its answer could not be read, the clock ran out
    or the page cap cut the paging short. `unread` is then the link of the page we did not
    get, so the next run carries on from there instead of reading the whole window again;
    the caller keeps it in the cursor. Everything read before that page is already in
    `records` and is kept -- a run never throws away what it earned.
    """
    scanned = on_theme = pages = 0
    complete = True
    stopped_early = False
    unread = None               # the page we did not get to: the caller stores it and resumes
    while url and pages < MAX_PAGES:
        left = deadline - time.monotonic()
        if left < MIN_PAGE_S:
            log.warning(f"{source}: the run's time ran out with more still to read; the next run "
                        f"carries on from this very page, not from the start of the window")
            complete = False
            stopped_early = True
            unread = url
            break
        try:
            answer = _fetch_bounded(url, min(PAGE_TIMEOUT_S, left))
        except Exception as e:
            answer, why = None, f"{type(e).__name__}: {e}"[:140]
        if answer is None:
            log.warning(f"{source}: {_why_in_words(why)}; the window stays open and will be "
                        f"read again next run")
            complete = False
            stopped_early = True
            unread = url
            break
        if not answer.ok:
            log.warning(f"{source}: the feed answered {answer.status}; the window stays open "
                        f"and will be read again next run")
            complete = False
            stopped_early = True
            unread = url
            break
        try:
            data = _json(answer)
        except Exception as e:
            log.warning(f"{source}: the answer could not be read ({type(e).__name__}); the window "
                        f"stays open and will be read again next run")
            complete = False
            stopped_early = True
            unread = url
            break
        releases = data.get("releases") or []
        pages += 1
        for rel in releases:
            scanned += 1
            row = classify(rel, source)
            if not row or not row["ocid"]:
                continue
            on_theme += 1
            if row["ocid"] in seen:
                continue
            if row["tier"] == 3 and not INCLUDE_TIER_3:
                log.skipped(row["ocid"], "tier 3 (testing is only a line item); held by the module's knob")
                continue
            if row["stage"] not in ("open", "planning"):
                log.skipped(row["ocid"], f"the notice is {row['stage']}, not an open opportunity")
                continue
            if row.get("deadline") and (a_date(row["deadline"]) or "") < now_day:
                log.skipped(row["ocid"], f"its deadline {a_date(row['deadline'])} has passed")
                continue
            try:
                records.append(_to_bid(row, source))
            except ContractError as e:
                records.append(e.record)
            seen.add(row["ocid"])
        url = (data.get("links") or {}).get("next")
    if url and not stopped_early:
        unread = url
        # Still a next page and nothing else stopped us: the cap cut the window short.
        # Until 2026-11-17 this spoke for EVERY early exit, so a feed that simply did not
        # answer was also reported as "stopped at the page cap" -- two alarms for one fault,
        # and the second one false. A warning must name what actually happened.
        log.warning(f"{source}: stopped at the {MAX_PAGES}-page cap with more still to read; "
                    f"the window stays open and will be read again next run")
        complete = False
    return scanned, on_theme, complete, unread


def find(context):
    now = context["now"]
    now_day = a_date(now)
    cursor = the_cursor(context, {}) or {}
    records, seen = [], set()

    started = time.monotonic()
    fts_deadline = started + RUN_BUDGET_S * FTS_SHARE
    cf_deadline = started + RUN_BUDGET_S

    # Find a Tender — the whole window, no stage filter, day by day, resumable page by page.
    # The cursor key is new ("fts2"): the old one ("fts") stands at the days read through the
    # broken filter, and those days have to be read again. FTS_READ_FROM is where that
    # re-reading starts; it is deliberately a day earlier than this module's first ever run.
    fts = dict(cursor.get("fts2") or {})
    fts_from = fts.get("through") or FTS_READ_FROM
    carried = fts.get("next")       # a page left half way through the first day, from last run
    fts_floor = _days_before(now, MAX_LOOKBACK_DAYS)
    if fts_from < fts_floor:
        log.warning(f"find-a-tender: the window had stood open since {fts_from[:10]}; reading only "
                    f"from {fts_floor[:10]} (the {MAX_LOOKBACK_DAYS}-day cap). Notices updated "
                    f"between {fts_from[:10]} and {fts_floor[:10]} are NOT read by this module and "
                    f"never will be -- say so to whoever watches this feed")
        fts_from = fts_floor
        carried = None
    # Day by day, oldest first. A wide window is one huge page -- 12 days of Find a Tender is
    # 1.6 MB and 28 s on a good minute, and on a bad one (17 Nov: 502s and no answer at all) it
    # cannot be read at all, so the window stays open, grows, and the feed never recovers. Day
    # slices are small, and -- the point -- each day that IS read moves the mark forward, so a
    # run that dies half way keeps what it earned instead of starting over tomorrow.
    fts_scanned = fts_theme = 0
    fts_through = fts_from
    fts_ok = True
    fts_left = None                 # the page the next run starts from, if we stop part way
    resumed = bool(carried)
    from_page = carried             # the page THIS run began on, if it resumed one
    for a, b in _day_slices(fts_from, now):
        url = carried or f"{FTS_API}?updatedFrom={a}&updatedTo={b}&limit={PAGE_LIMIT}"
        carried = None              # only the first day of this run can be a resumed one
        scanned, theme, ok, unread = _pull(url, "find-a-tender", now_day, records, seen,
                                           fts_deadline)
        fts_scanned += scanned
        fts_theme += theme
        if not ok:
            fts_ok = False
            # Keep the page to carry on from -- unless it is the very page this run resumed
            # on and it failed again. A link that cannot be read twice is not worth keeping:
            # dropping it makes the next run read that day from its first page, which costs
            # pages but can never strand a day behind a link the feed will not serve.
            fts_left = None if (unread and unread == from_page) else unread
            if from_page and fts_left is None:
                log.warning("find-a-tender: the page carried over from the last run failed "
                            "again, so it is dropped and this day is read afresh next run")
            break
        fts_through = b
    log.read(f"Find a Tender, updated {fts_from[:10]}..{fts_through[:10]}"
             f"{' (carried on from the page the last run stopped at)' if resumed else ''}"
             f" ({fts_theme} on-theme"
             f"{'' if fts_ok else f', READ INCOMPLETE -- read in full only up to {fts_through[:10]}'
                                  f'{", and the next run carries on mid-day" if fts_left else ""}'})",
             fts_scanned)

    # Contracts Finder, read the same way, by published date. Its publishedTo is EXCLUSIVE
    # (from=16 to=16 answers nothing, from=16 to=17 answers the 16th), so a day is [d, d+1).
    cf_from = cursor.get("cf") or now_day_minus(now_day, FIRST_RUN_DAYS)
    cf_floor = now_day_minus(now_day, MAX_LOOKBACK_DAYS)
    if cf_from < cf_floor:
        log.warning(f"contracts-finder: the window had stood open since {cf_from}; reading only from "
                    f"{cf_floor} (the {MAX_LOOKBACK_DAYS}-day cap). Updates between those two dates "
                    f"are NOT read -- the feed was unreadable for that long")
        cf_from = cf_floor

    cf_scanned = cf_theme = 0
    cf_through = None
    cf_ok = True
    day = cf_from
    while day <= now_day:
        day_ok = True
        for stage in CF_STAGES:
            url = (f"{CF_API}?publishedFrom={day}&publishedTo={_day_after(day)}"
                   f"&size={PAGE_LIMIT}&stages={stage}")
            scanned, theme, ok, _ = _pull(url, "contracts-finder", now_day, records, seen,
                                           cf_deadline)
            cf_scanned += scanned
            cf_theme += theme
            day_ok = day_ok and ok
        if not day_ok:
            cf_ok = False
            break
        cf_through = day
        day = _day_after(day)
    log.read(f"Contracts Finder, published {cf_from}..{cf_through or 'nothing'} ({cf_theme} on-theme"
             f"{'' if cf_ok else f', READ INCOMPLETE -- got no further than {cf_through or cf_from}'})",
             cf_scanned)

    # The mark moves exactly as far as the feed was actually read -- no further, and never back.
    # Until 2026-11-16 it jumped to `now` whatever happened, so one failed read skipped that
    # window for good and the run still looked like a quiet day. Then it moved only on a perfect
    # read, which was safe but meant a feed having a bad week made no progress at all. Now each
    # day read in full is kept, and the day that failed is where tomorrow starts.
    moved = dict(cursor)
    was = (cursor.get("fts2") or {}).get("through") or ""
    moved["fts2"] = {"through": max(fts_through, was), "next": fts_left}
    if cf_through and cf_through > (cursor.get("cf") or ""):
        moved["cf"] = cf_through
    return {"records": records, "cursor": moved}


def _day_after(day):
    return now_day_minus(day, -1)


def _day_slices(from_iso, to_iso):
    """The span split into day-sized windows, oldest first, as (start, end) pairs."""
    from datetime import datetime, timedelta, timezone

    def dt(x):
        d = datetime.fromisoformat(x.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    a, end = dt(from_iso), dt(to_iso)
    out = []
    while a < end:
        b = min(a + timedelta(days=1), end)
        out.append((a.strftime("%Y-%m-%dT%H:%M:%SZ"), b.strftime("%Y-%m-%dT%H:%M:%SZ")))
        a = b
    return out


def _days_before(iso, days):
    from datetime import datetime, timedelta, timezone
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    d = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) - timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_day_minus(day, days):
    from datetime import date, timedelta
    return (date.fromisoformat(day) - timedelta(days=days)).isoformat()
