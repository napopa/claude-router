---
name: router-analytics
description: Generate HTML analytics dashboard for routing statistics
---

# /router-analytics Command

Generate a visual HTML analytics dashboard from your routing statistics.

## Usage

```
/router-analytics
/router-analytics --output ~/Desktop/router-report.html
```

## What It Does

Reads `~/.claude/router-stats.json` and generates an HTML dashboard with:
- Route distribution pie chart
- Daily/weekly trends line chart
- Queries per session over time
- Optimization rate tracking

## Output

By default generates `router-analytics.html` in the current directory.

## Requirements

Run some queries through the router first to build up statistics.
