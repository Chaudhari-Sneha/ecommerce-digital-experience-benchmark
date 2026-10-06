# Power BI Dashboard

The dashboard is a **Power BI Project** (`DigitalFriction.pbip`). Instead of a
single binary `.pbix` file, the report and data model are saved as plain text
files, so every change can be tracked in git.

## Open and refresh

1. Make sure the database exists and the pipeline has run at least once (see
   the main [README](../README.md)).
2. Open `DigitalFriction.pbip` in Power BI Desktop. Double-click it, or use
   **File → Open** in Desktop if Windows hasn't associated `.pbip` files with
   Power BI.
3. Click **Home → Refresh**.
4. The first time you refresh, Desktop asks for PostgreSQL credentials. Choose
   **Database** and enter the user and password from your `.env` file. If it
   then says the connection can't be encrypted, click **OK**. That's expected
   for a database running on your own PC.

After that, click **Refresh** whenever you want the latest data the scheduled
pipeline has collected.

## Pages

- **Executive Dashboard**: headline KPIs, then three sections:
  - Priority & Attention
  - Competitive Position
  - Drivers & Trends

  The company slicer only filters the Drivers & Trends section, so the
  industry comparisons above it always show every company.
- **Data Quality & Methodology**: data freshness, run history, collection
  status per source, data completeness and how the scores are calculated.

Some visuals need history before they fill in. Anomaly monitoring starts once a
company has 7 reliable days of data, and the sustained-vs-one-off trend status
needs about a week. Until then they show a short message explaining why.

Scores built from too little data (under 60% completeness) are left out of
rankings and averages. Companies removed from the tracked list are hidden.

## Colours

The five score components use the same colour everywhere:

- Performance: blue
- Core Web Vitals: orange
- Site Quality: aqua
- Reliability: yellow
- Broken Links: magenta

The order comes from a colourblind-safe palette. Industry average lines are
grey.

## Editing

Edit visuals in Power BI Desktop as usual and save with **File → Save**. The
data model reads straight from the SQL views in `sql/`, so if you add or rename
a column in a view, run `python scripts/setup_db.py` first, then refresh in
Desktop.

## Troubleshooting

- **Saving fails or files conflict.** This happens if the project sits in a
  OneDrive-synced folder. Pause OneDrive sync while editing, or move the project
  folder outside OneDrive.
- **"This version of Power BI does not support the version you have
  provided."** Your Power BI Desktop is older than the file format. Update it
  from the Microsoft Store.
- **Refresh fails with a precision or type error.** A view is probably
  returning an unbounded `NUMERIC`. Cast it to `float8` in the view. All the
  existing views already do this.
