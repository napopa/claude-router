---
name: router-stats
description: Display Claude Router usage statistics (global across all projects)
---

# /router-stats Command

Display usage statistics from Claude Router.

**Note:** Stats are global - they track routing across all your projects.

## Usage

```
/router-stats
```

## Instructions

1. Read the stats file at `~/.claude/router-stats.json`
2. If the file doesn't exist, inform the user that no stats are available yet
3. Calculate percentages for route distribution
4. Calculate **optimization rate**: percentage of queries routed to Haiku or Sonnet instead of Opus
5. Format and display the statistics

## Data Format

The stats file contains:
```json
{
  "version": "1.3",
  "total_queries": 100,
  "routes": {"fast": 30, "standard": 50, "deep": 10, "orchestrated": 10},
  "exceptions": {"router_meta": 15, "slash_commands": 0},
  "tool_intensive_queries": 25,
  "orchestrated_queries": 10,
  "sessions": [
    {
      "date": "2026-01-03",
      "queries": 25,
      "routes": {"fast": 8, "standard": 12, "deep": 2, "orchestrated": 3}
    }
  ],
  "last_updated": "2026-01-03T15:30:00"
}
```

## Output Format

Present the stats like this:

```
Claude Router Statistics (Global)
==================================

All Time
--------
Total Queries Routed: 100
Optimization Rate: 80% (queries classified to cheaper models)

Route Distribution:
  Fast (Haiku):      30 (30%)
  Standard (Sonnet): 50 (50%)
  Deep (Opus):       10 (10%)
  Orchestrated:      10 (10%)

Today
-----
Queries: 25
Routes: Fast 8 | Standard 12 | Deep 2 | Orchestrated 3
```

## Why This Matters for Subscribers

If you're on Claude Pro or Max, routing to smaller models:

- **Extended usage limits** - Smaller models use less of your monthly capacity
- **Longer sessions** - Less context consumed means fewer auto-compacts
- **Faster responses** - Haiku responds 3-5x faster than Opus

## Metrics Explained

- **Optimization Rate**: Percentage of queries classified to Haiku or Sonnet instead of Opus. Reflects classification decisions, not confirmed execution.
- **Route Distribution**: How queries were classified across models.
