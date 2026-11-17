"""UK public tenders for software testing / QA / performance / data masking.

One module, two official OCDS feeds, one sieve:

  * Find a Tender      — the large UK register (threshold and above)
      https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages  (updatedFrom/updatedTo)
  * Contracts Finder   — the smaller notices below that threshold
      https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search  (publishedFrom/publishedTo)

Both answer OCDS JSON (releases[] + links.next) and are read straight through `fetch` — no
browser, no key, no AI. Each is asked for one procurement stage at a time (tender, then
planning), so a run reads the open opportunities instead of the whole day's traffic. The sieve is pure rules (CPV code + wording, with vetoes), so a run is
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
import time
from tommos_module import ContractError, a_bid, a_date, a_key, fetch, log, the_cursor

# ----------------------------------------------------------------------------- knobs
INCLUDE_TIER_3 = True       # tier 3 = widest match; emitted, Tommo Bids triages it downstream
FIRST_RUN_DAYS = 7          # how far back the very first run looks (no cursor yet)
MAX_PAGES = 12              # page cap per feed, so one run stays inside the time limit

# Both feeds answer every notice updated in the window -- awards, amendments, contract
# records, the lot. The sieve then throws away everything that is not an open opportunity,
# so a whole day of Find a Tender arrived as 664 releases over 7 pages to keep 12 of them.
# Asking the feed for the stages we actually want turns that into one page of 12 in under
# two seconds (measured 2026-11-17). The two feeds take ONE stage per call -- a repeated
# or comma-joined `stages` keeps only the last -- so each feed is read once per stage.
STAGES = ("tender", "planning")

# Tommos ends a run at 120 s. Find a Tender can be slow (17-31 s a page, and on a bad day
# it does not answer at all), and the SDK's own retrying would spend 90 s on a single page
# before giving up -- which is exactly how the 17 Nov 08:00 run died with "Out of time"
# and nothing read. So: one try per page, a timeout that never outlives the budget, and a
# wall clock that stops the paging in time for the other feed to be read at all. A window
# cut short this way is INCOMPLETE -- its cursor stays put and it is read again next run.
RUN_BUDGET_S = 100          # wall clock the whole run may spend fetching
FTS_SHARE = 0.6             # of it, at most this much goes to Find a Tender
PAGE_TIMEOUT_S = 35         # longest wait for one page
MIN_PAGE_S = 5              # do not start a page with less time left than this

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


def _pull(url, source, now_day, records, seen, deadline):
    """Page a feed from `url`, classify each release, append bids.

    Returns (scanned, on_theme, complete). `complete` is False when the window was NOT
    read to its end -- the feed errored, its answer could not be read, or the page cap
    cut the paging short. The caller must then leave that source's cursor where it was,
    so the unread window is read again next run instead of being skipped for good.
    """
    scanned = on_theme = pages = 0
    complete = True
    while url and pages < MAX_PAGES:
        left = deadline - time.monotonic()
        if left < MIN_PAGE_S:
            log.warning(f"{source}: the run's time ran out with more still to read; the window "
                        f"stays open and will be read again next run")
            complete = False
            break
        try:
            answer = fetch(url, tries=1, timeout_s=max(1, int(min(PAGE_TIMEOUT_S, left))))
        except Exception as e:
            log.warning(f"{source}: the feed could not be reached ({type(e).__name__}); the window "
                        f"stays open and will be read again next run")
            complete = False
            break
        if not answer.ok:
            log.warning(f"{source}: the feed answered {answer.status}; the window stays open "
                        f"and will be read again next run")
            complete = False
            break
        try:
            data = _json(answer)
        except Exception as e:
            log.warning(f"{source}: the answer could not be read ({type(e).__name__}); the window "
                        f"stays open and will be read again next run")
            complete = False
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
    if url:
        # Still a next page when the loop stopped: the cap cut the window short.
        log.warning(f"{source}: stopped at the {MAX_PAGES}-page cap with more still to read; "
                    f"the window stays open and will be read again next run")
        complete = False
    return scanned, on_theme, complete


def find(context):
    now = context["now"]
    now_day = a_date(now)
    cursor = the_cursor(context, {}) or {}
    records, seen = [], set()

    started = time.monotonic()
    fts_deadline = started + RUN_BUDGET_S * FTS_SHARE
    cf_deadline = started + RUN_BUDGET_S

    # Find a Tender — incremental by last-updated time, one call per stage.
    fts_from = cursor.get("fts") or _days_before(now, FIRST_RUN_DAYS)
    fts_scanned = fts_theme = 0
    fts_ok = True
    for stage in STAGES:
        url = f"{FTS_API}?updatedFrom={fts_from}&updatedTo={now}&stages={stage}"
        scanned, theme, ok = _pull(url, "find-a-tender", now_day, records, seen, fts_deadline)
        fts_scanned += scanned
        fts_theme += theme
        fts_ok = fts_ok and ok
    log.read(f"Find a Tender, updated {fts_from[:10]}..{now_day} ({fts_theme} on-theme"
             f"{'' if fts_ok else ', READ INCOMPLETE'})", fts_scanned)

    # Contracts Finder — by published date (whole-day granularity), one call per stage.
    cf_from = cursor.get("cf") or now_day_minus(now_day, FIRST_RUN_DAYS)
    cf_scanned = cf_theme = 0
    cf_ok = True
    for stage in STAGES:
        url = f"{CF_API}?publishedFrom={cf_from}&publishedTo={now_day}&size=100&stages={stage}"
        scanned, theme, ok = _pull(url, "contracts-finder", now_day, records, seen, cf_deadline)
        cf_scanned += scanned
        cf_theme += theme
        cf_ok = cf_ok and ok
    log.read(f"Contracts Finder, published {cf_from}..{now_day} ({cf_theme} on-theme"
             f"{'' if cf_ok else ', READ INCOMPLETE'})", cf_scanned)

    # The cursor moves ONLY for a feed that was read to the end. A feed that errored, answered
    # something unreadable, or was cut by the page cap keeps its old mark, so its window is read
    # again next run. Before 2026-11-16 the cursor jumped to `now` whatever happened: one failed
    # read silently skipped that window for good, and the run still looked like a quiet day with
    # "0 found". A failed source must never be indistinguishable from an empty one.
    moved = dict(cursor)
    if fts_ok:
        moved["fts"] = now
    if cf_ok:
        moved["cf"] = now_day
    return {"records": records, "cursor": moved}


def _days_before(iso, days):
    from datetime import datetime, timedelta, timezone
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    d = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) - timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_day_minus(day, days):
    from datetime import date, timedelta
    return (date.fromisoformat(day) - timedelta(days=days)).isoformat()
