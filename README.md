# UK tenders — a Tommos module

Open UK public tenders for **software testing, QA, performance / load testing and data masking**,
found for a Tommos workspace from the two official registers and handed to the door of **Bids**.

Built on the [Tommos module template](https://github.com/TommosCRM/tommos-module-template):
one function, `find`, run by Tommos on a schedule, in a sandbox, checked record by record.

## The two feeds

| Feed | What it holds | Endpoint (OCDS) |
| --- | --- | --- |
| Find a Tender | the large UK register, at and above the threshold | `www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages` (`updatedFrom` / `updatedTo`) |
| Contracts Finder | the smaller notices below that threshold | `www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search` (`publishedFrom` / `publishedTo`) |

Both answer OCDS JSON (`releases[]` with a `links.next`), are public, and are read straight through
the SDK's `fetch` — **no browser, no key, no model**. Each bid carries its `source`
(`find-a-tender` or `contracts-finder`).

## The sieve

Pure rules, so a run is deterministic and free. A notice is kept only when its **main** CPV code
and its wording agree, with vetoes on neighbouring trades that borrow the word "testing":

- **Tier 1** — main CPV `72254` (software testing); or an IT/software code (`72`/`48`) with a strong
  software-testing phrase in the text; or an IT code carrying `72254` with QA wording.
- **Tier 2** — an IT/software code with quality-assurance / testing wording.
- **Tier 3** — a software-testing phrase in a **non-IT** procurement (the widest match).

Vetoes (checked on the title, so a sector name never vetoes): construction / civil works,
clinical & laboratory testing, security-only (penetration / cyber), licence & subscription buys,
learning platforms, staffing-body CPVs, transparency/direct-award notices.

Each bid also carries:
- **`summary`** — the one sentence that says *what must be done* (the kind of work).
- **`matched`** — the rule that took it, with the verbatim evidence quote; for a tier-1/2 notice
  where testing is only a line item, it says so ("testing is a line item, not the main subject").
- **`opp_type`** — `Open` / `Planning`, or, for a framework/DPS, a note that the value is a ceiling.

## Knobs (top of `find.py`)

| Constant | Default | Meaning |
| --- | --- | --- |
| `INCLUDE_TIER_3` | `True`  | emit the widest (tier-3) matches, or hold them |
| `FIRST_RUN_DAYS` | `7` | how far back the very first run (no cursor) looks |
| `MAX_PAGES` | `12` | page cap per feed, so a run stays inside the time limit |
| `MAX_LOOKBACK_DAYS` | `30` | the furthest back a window left open by failed reads may reach |
| `FTS_READ_FROM` / `CF_READ_FROM` | `2026-11-10` | the one-off re-reading an upgrade does (see below) |
| `RUN_BUDGET_S` / `PAGE_TIMEOUT_S` | `175` / `90` | the run's own clock, and the longest wait for one page |

## How the two feeds are read

Each feed is asked **one day at a time**, and each day read in full moves that feed's mark forward,
so a run cut short keeps what it earned. A day too large for one run is resumed on the very page it
stopped at. Find a Tender is read **without** the `stages` filter: that filter returns only notices
published under the old EU directives and hides almost everything published under the Procurement
Act 2023 (measured 20 November 2026 — it hid 71 of a day's 83 open notices). Contracts Finder's
filter of the same name is exact (measured, nothing lost, nothing extra) and is used.

A day the feed answers with **nothing at all** is not believed on its own: both services return a
well-formed but empty package now and then while nothing is wrong with the request. The module then
asks for the whole week before that day, unfiltered. If the week holds a notice, the feed is
answering and the quiet day is real; if the week is empty too, the day is left unread for the next
run. A genuinely quiet day does exist — Contracts Finder publishes nothing on a Sunday.

**A day of Find a Tender takes longer to read than a whole run.** Measured 20 November 2026 on
the day 10 November: a page of 100 notices costs 86.7 s (912 KB), and about 35 s of that is the
asking itself, so smaller pages are far more expensive per notice — and 100 is the most the feed
allows (150 and 200 are refused). That day held 407 notices: four or five pages, some 350 s, where
a run has 200 s in all. So a mark left days behind can never be caught up by reading it in order.

The module therefore reads in two halves. The **front** is the newest days (`LAG_DAYS` = 2) and is
read first, with the larger part of the time. Anything older becomes the **backlog**, read behind
the front with what is left — never starved, never in the way. The backlog disappears from the
cursor once it is read in full, and the run says so. Because a run reads roughly one page of each
half, **how often the source runs is what sets the pace**: every 2 hours keeps the feed current
with room to spare and clears a week of backlog in about three days.

Marks are kept under the keys `fts2` and `cf2`; the backlog lives in `fts2.lag`. An installation upgrading from v0.1.7 or earlier
(which marked its progress under `fts` / `cf`) re-reads from `FTS_READ_FROM` / `CF_READ_FROM`, so
the notices the older versions lost come in by themselves; the old keys are then dropped. A
brand-new installation simply reads `FIRST_RUN_DAYS` back.

Only **open** and **planning** notices are emitted; awarded / complete / cancelled, and anything
past its deadline, are skipped with a reason in the log.

## What the module does not do

- **History.** It keeps Tommos current from today forward (incremental by cursor). The back-catalogue
  of a whole year lives in the OCDS yearly archives and is a separate, one-time import if ever wanted.
- **Triage.** It finds candidates; Tommo Bids decides "Ours / Not ours" downstream.

## Develop

```sh
npm install
pip install -r requirements.txt
npx tommos-module check . --entry find.py
```

The example `examples/a-week-of-uk-tenders/` is a real one-week snapshot of both feeds (trimmed):
two tier-1 open tenders are emitted (an ECMS buy and a Home Office testing DPS), two tier-3 awarded
notices are skipped.

## Give it to the workspace

Push a tag (`git tag v0.1.0 && git push origin v0.1.0`), then in Tommos **Settings → Sources →
Add a module** give the repository link and the tag. The source starts in **Trial**; promote it to
**Live** after reading its trial report. No secrets to fill — the module needs none.
