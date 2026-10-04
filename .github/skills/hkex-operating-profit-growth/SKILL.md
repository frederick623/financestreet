---
name: hkex-operating-profit-growth
description: Find HKEX-listed companies whose newly released quarterly, interim, or annual results report operating profit growth above 20% versus the comparable prior-period report.
---

# HKEX Operating Profit Growth

Use the `hkex-news` MCP server configured in `.mcp.json`. It runs
`hkex_mcp.py` against the local HKEX announcement database.

## When to use

Use this skill when asked to screen recent HKEX results announcements for
companies reporting operating profit growth above a specified threshold. Unless
the user supplies different values, use:

- the latest ten calendar days in Hong Kong time, excluing today;
- a strict growth threshold of more than 20%; and
- quarterly, interim, and annual results only.

## Workflow

1. Determine the inclusive date range as today minus six days through today,
   using `Asia/Hong_Kong`.
2. Call `database_status` before searching. The
   `latest_release_time` and the current-security `last_to_date` must cover the
   requested end date. If they do not, state that the local data is stale and
   report the date through which it is available; do not imply the result is a
   complete latest-seven-day screen.
3. Call `search_news` for the date range and find results announcements. Search
   all relevant title variants, including:
   - `annual results`
   - `final results`
   - `interim results`
   - `half year results`
   - `quarterly results`
   - `first quarter results`
   - `third quarter results`
4. Use `category` to restrict searches to results announcements when the stored
   category supports that restriction. Use the maximum permitted limit where
   necessary. Combine all searches and deduplicate records by `news_id`.
5. Exclude notices that are not actual financial results, such as meeting
   notices, board-meeting dates, publication notices, supplemental notices,
   corrections without financial figures, and annual/interim reports that
   merely duplicate an already screened results announcement.
6. Call `get_news` for every candidate and inspect `document_text`. Do not
   decide from the announcement title or summary alone.
7. Identify the current and comparative operating-profit figures from the same
   announcement. Accept clearly equivalent issuer labels such as:
   - operating profit;
   - profit from operations; or
   - operating profit before separately disclosed items, only when the same
     basis is shown for both periods.
8. Compare like-for-like periods and accounting bases:
   - quarterly results against the corresponding prior-year quarter;
   - interim or half-year results against the corresponding prior-year interim
     period; and
   - annual or final results against the corresponding prior financial year.
9. Do not substitute revenue, EBITDA, adjusted EBITDA, gross profit, profit
   before tax, net profit, or profit attributable to shareholders for operating
   profit. If the issuer does not disclose an operating-profit measure with a
   comparable prior-period value, omit it.
10. Normalize both figures to the same currency, unit, and sign. Prefer the
    consolidated income statement figures. If figures are restated, use the
    restated comparator and say so in the summary.
11. Calculate:

    `growth = ((current operating profit - previous operating profit) / previous operating profit) * 100`

12. Include a company only when both values are numeric, the previous operating
    profit is greater than zero, and the calculated growth is strictly greater
    than 20%. A move from an operating loss or zero to a profit is a turnaround,
    not a meaningful percentage increase, so do not include it in the qualifying
    table.
13. Read management commentary, segment discussion, or the results summary for
    stated reasons for the increase. Summarize only explanations supported by
    the announcement. If none is provided, write `Not stated`.
14. If one announcement covers multiple stock codes for the same issuer, keep
    the codes together in one row. If an issuer released multiple qualifying
    reporting periods in the window, use one row per report.

## Output

Begin with the exact date range searched and whether the local database was
current through the end date. Sort qualifying rows by growth descending.

| Stock code | Company name | Report | Operating profit | Growth | Growth summary |
|---|---|---|---:|---:|---|
| 00000 | Example Limited | Annual results for year ended YYYY-MM-DD | HKD 125m (previous: HKD 100m) | 25.0% | Higher sales and improved product mix, as stated by management. |

For `Report`, identify the report type and period end. For `Operating profit`,
show the current value followed by the comparable previous value in
parentheses, including currency and unit. Round growth to one decimal place,
but apply the threshold using the unrounded calculation.

After the table, add a short **Methodology and caveats** note that:

- says growth was independently calculated from the disclosed figures;
- identifies any accepted equivalent label used instead of `operating profit`;
- notes missing or unreadable `document_text` records that could make the
  screen incomplete; and
- includes the HKEX `document_url` as a Markdown link in the Report cell or in
  the caveat for each qualifying announcement.

If nothing qualifies, still report the searched date range, database freshness,
and any coverage limitations, then state: `No qualifying companies found.`
Never invent a value, infer an undisclosed operating profit, or describe the
screen as exhaustive when the database is stale or candidate documents could
not be read.
