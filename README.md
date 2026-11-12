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
