---
name: router-stats
description: Display Claude Router usage statistics and cost savings
user_invokable: true
---

# Router Stats

Display usage statistics from Claude Router.

## Instructions

Read the stats file at `~/.claude/router-stats.json` and present the data in a clear, formatted way.

## Data Format

The stats file contains (v1.3 schema):
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
╔═══════════════════════════════════════════════════╗
║           Claude Router Statistics                 ║
╚═══════════════════════════════════════════════════╝

📊 All Time
───────────────────────────────────────────────────
Total Queries Routed: 100
Optimization Rate: 80% (queries routed to cheaper models)

Route Distribution:
  Fast (Haiku):       30 (30%)  ████████░░░░░░░░░░░░
  Standard (Sonnet):  50 (50%)  ██████████████░░░░░░
  Deep (Opus):        10 (10%)  ████░░░░░░░░░░░░░░░░
  Orchestrated:       10 (10%)  ████░░░░░░░░░░░░░░░░

🔧 Tool-Aware Routing
───────────────────────────────────────────────────
Tool-Intensive Queries: 25 (25%)
Orchestrated Queries:   10 (10%)

⚡ Exceptions (handled by Opus despite classification)
───────────────────────────────────────────────────
Router Meta-Queries:  15  (queries about the router itself)
Total Exceptions:     15

📅 Today (2026-01-03)
───────────────────────────────────────────────────
Queries: 25

Route Distribution:
  Fast: 8 | Standard: 12 | Deep: 2 | Orchestrated: 3
```

## Steps

1. Use the Read tool to read `~/.claude/router-stats.json`
2. If the file doesn't exist, inform the user that no stats are available yet
3. Calculate percentages for route distribution
4. Calculate **optimization rate**: percentage of queries routed to Fast or Standard (not Deep/Orchestrated)
5. Display exception counts if present
6. Format and display the statistics

## Notes

- The stats file only contains factual route counts — no accumulated savings. The router classifies queries but cannot verify that the routing directive was actually followed, so reporting dollar savings would be misleading.
- **Optimization rate** is the percentage of queries classified to a cheaper model (Haiku or Sonnet). It reflects classification decisions, not confirmed execution.
- **Exceptions**: Queries about the router itself are classified but handled by Opus (per CLAUDE.md rules). This is intentional - users discussing the router get the most capable model while still seeing what the classifier decided.
