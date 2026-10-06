# Digital Friction Score: Indian E-commerce Benchmarking

How hard is it to shop on India's biggest e-commerce sites, and who is getting
better or worse?

This project answers that with an automated pipeline. Twice a day it collects
public digital-experience data for 10 Indian e-commerce companies, turns it
into a single explainable **Digital Friction Score**, flags unusual changes and
ranks where management should look first. A Power BI dashboard sits on top.

Everything runs on free tools: Python, a local PostgreSQL database and Power BI
Desktop. There are no paid APIs, no cloud services and no Power BI licence.

## How it works

```
Google PageSpeed Insights API ─┐
Uptime checker                 ├─▶ Python collectors ─▶ PostgreSQL ─▶ SQL views ─▶ Power BI
Broken-link checker           ─┘   (Windows Task Scheduler, every 12 hours)
```

Each run:

1. **Collects** three kinds of data for every company:
   - a Google Lighthouse test (mobile and desktop) plus real-user Chrome UX
     Report data, both through the PageSpeed Insights API
   - a direct homepage availability and response-time check
   - a scan of up to 40 links on the homepage for broken ones
2. **Stores** the raw results in PostgreSQL, along with a JSON copy in
   `data/raw/<date>/` as an audit trail.
3. **Scores** each company for the day (India time) and checks the new score
   against that company's own history for anomalies.
4. **Logs** the outcome of every source for every company, so the dashboard can
   show how complete and fresh the data is.

All the analysis lives in SQL views, and Power BI reads those views directly.

## Companies tracked

Flipkart, Myntra, FirstCry, Nykaa, Meesho, Purplle, Tata CLiQ, Amazon India,
Reliance Digital and Croma. The list lives in `config/companies.yaml`. If you
remove a company it is marked inactive, and its history is kept.

Snapdeal, BigBasket and Ajio were in the original list but were replaced
because no comparable data could be collected for them. Snapdeal and BigBasket
block Google's Lighthouse crawler, and Lighthouse would have scored their error
pages as a perfect 100. Ajio timed out in every Lighthouse run.

## The dashboard

The Power BI report has two pages.

![Executive Dashboard](images/executive-dashboard.png)

**Executive Dashboard**: one page for decision-makers. A row of headline KPIs
sits above three sections.

| Section | What it answers |
|---|---|
| Headline KPIs | Industry average friction, how many companies are worse than average, the top priority, the best and worst performer, and how much to trust today's data |
| Priority & Attention | Who needs attention first (priority matrix: market position vs recent trend), what each company's score is made of, and where each one trails the industry |
| Competitive Position | Real-user experience on Google's Core Web Vitals, mobile vs desktop performance, and whether slow loading is a server problem or a page problem |
| Drivers & Trends | Friction over time vs the industry, which components are getting worse (sustained vs one-off), the specific Lighthouse findings slowing each site, and anomaly alerts. A company slicer filters this section only |

**Data Quality & Methodology**: data freshness, last-run success, run history,
collection status per company and source, daily data completeness, and a short
explanation of how the scores are calculated.

![Data Quality & Methodology](images/data-quality.png)

Opening and refreshing the dashboard is covered in
[powerbi/SETUP_GUIDE.md](powerbi/SETUP_GUIDE.md).

## Digital Friction Score

A score from 0 to 100 where **higher means more friction**, so lower is better.
It is a weighted sum of five components, each first converted to a 0-100
friction scale:

| Component | Weight | Based on |
|---|---|---|
| Performance | 25% | Lighthouse performance score (60% mobile, 40% desktop) |
| Core Web Vitals | 20% | LCP, CLS and a lab responsiveness measure, against Google's good/poor thresholds |
| Site Quality | 15% | Lighthouse accessibility, best-practices and SEO scores |
| Reliability | 25% | Uptime and server response time |
| Broken Links | 15% | Share of homepage links that are broken |

Because it is a plain weighted sum, every score splits exactly into
per-component contributions, which is how the dashboard explains any change.
If a component can't be measured for a company, it is left out and the other
weights are scaled up, so a site is never rewarded for being unmeasurable. A
score counts as reliable only when at least 60% of the expected data arrived.
The full formula is in `collectors/scoring.py`.

## Management Priority Score

A 0-100 score that ranks **where to look first**. It is a prioritisation aid,
not a business case: it does not estimate revenue, conversion or customer
loss, and it makes no causal claims.

```
Priority = Confidence × (0.35 × Severity + 0.25 × Gap + 0.25 × Deterioration + 0.15 × Persistence)
```

| Signal | Meaning (each scaled to 0-100) |
|---|---|
| Severity | The company's latest reliable friction score |
| Gap | How far it is above the industry average (20+ points above = 100) |
| Deterioration | How much worse it is than its own 7-day average (10+ points worse = 100) |
| Persistence | Share of the last 14 days it spent above the industry average |
| Confidence | 0.5 + 0.5 × data completeness × min(1, readings in last 21 days ÷ 7) |

Deterioration compares against a 7-day average, not the previous reading,
because single Lighthouse runs are noisy. A site can move 15-20 points between
two runs with nothing actually changing.

Scores of 45 and above are **High** priority and 25-45 are **Medium**. The
priority matrix also places every company in one of four zones (**Critical**,
**Lagging**, **At Risk** or **Healthy**), based on whether it is worse than
the industry average and whether it is getting worse. Each company also gets a
main issue, a suggested area to investigate and a data-confidence label. The
recommendations are phrased as "review" or "investigate" because the data shows
where to look, not what caused the problem. The logic is in
`sql/views_management.sql`.

## Anomaly detection

Each new score is compared with the median of that company's reliable scores
from the previous 21 days, using a robust z-score (median and MAD, which are
not thrown off by a single bad run). A change is flagged only when it is at
least 3.5 robust z-scores **and** at least 2 points away from the median, so a
near-constant metric can't turn a tiny blip into an alert.

A company needs 7 reliable days of history before it is monitored. Until then
the dashboard says it is still building a baseline instead of guessing.

## Getting started

You need Python 3.12+, PostgreSQL and Power BI Desktop, all free. The scheduler
script is for Windows.

1. **Install the Python packages**
   ```
   pip install -r requirements.txt
   ```

2. **Get a free PageSpeed Insights API key.** In the
   [Google Cloud console](https://console.cloud.google.com/apis/credentials),
   enable the *PageSpeed Insights API* for a project and create an API key. No
   billing account is needed.

3. **Add your settings.** Copy `.env.example` to `.env` and fill in your
   PostgreSQL password and the API key. `.env` is git-ignored, so it stays on
   your machine.

4. **Create the database, tables and views**
   ```
   python scripts/setup_db.py
   ```
   This is safe to re-run.

5. **Run the pipeline once**
   ```
   python -m collectors.orchestrator
   ```
   This syncs the company list from `config/companies.yaml`, then collects and
   scores. A run takes several minutes because requests are spaced out
   politely. Progress goes to `logs/pipeline.log`.

6. **Schedule it** (optional, for continuous collection). In PowerShell:
   ```
   .\scripts\register_task_scheduler.ps1
   ```
   This registers a Windows scheduled task that runs the pipeline every
   12 hours and catches up if the PC was asleep. Use `-IntervalHours` to change
   the interval.

7. **Open the dashboard** and click **Refresh**. See
   [powerbi/SETUP_GUIDE.md](powerbi/SETUP_GUIDE.md).

## Tests

```
python -m unittest discover -s tests -v
```

The tests recompute the key numbers independently in Python and check that the
SQL views agree. They cover the friction-score breakdown, the priority score
and its tiers and zones, component trend status, real-user metrics, the load
time split, anomaly readiness and the anomaly rules. They read from the local
database and don't change anything.

## Project structure

```
collectors/          Data collection, scoring and anomaly detection (Python)
  orchestrator.py    Entry point for one pipeline run
config/              Companies to track
sql/                 Database schema and the analytical views
scripts/             Database setup and Task Scheduler registration
tests/               Checks of the SQL views against independent Python calculations
powerbi/             Power BI project (report and data model, saved as text)
data/raw/            Raw JSON from each run (not committed)
```

## Limitations

- **Bot protection is treated as "blocked", not "down".** Several sites reject
  automated requests. The pipeline records this honestly and doesn't try to
  get around it. For those sites, reliability is based on whether Google's
  Lighthouse could load the page and how fast the server responded.
- **The link check only sees links in the page HTML.** Amazon India and
  Tata CLiQ build their homepage links with JavaScript, so their Broken Links
  component is marked as unsupported rather than scored.
- **The responsiveness measure is a lab proxy.** Lighthouse can't measure real
  Interaction to Next Paint (INP). The real INP figure comes from Chrome UX
  Report data and appears separately on the dashboard.
- **The server vs page load split is approximate.** It subtracts one 75th
  percentile from another, and percentiles don't add up exactly.
- **History is still short.** Baselines, persistence and confidence get more
  stable as more days are collected.
- **Rankings are relative.** A High priority company is the first place to look,
  not proof of a business problem.
- **Refresh is manual.** Power BI Desktop pulls new data when you click
  Refresh. Automatic cloud refresh would need Power BI Service and a gateway,
  which this project deliberately avoids.

## License

Released under the [MIT License](LICENSE). The data comes from public sources:
Google's PageSpeed Insights API and the companies' own public homepages.
