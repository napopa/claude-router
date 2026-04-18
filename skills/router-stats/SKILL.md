---
name: router-stats
description: Display Claude Router usage statistics and cost savings
user_invokable: true
---

# Router Stats

Display usage statistics and estimated cost savings from Claude Router.

## Instructions

Read the stats file at `~/.claude/router-stats.json` and present the data in a clear, formatted way.

## Data Format

The stats file contains (v1.2 schema):
```json
{
  "version": "1.2",
  "total_queries": 100,
  "routes": {"fast": 30, "standard": 50, "deep": 10, "orchestrated": 10},
  "exceptions": {"router_meta": 15, "slash_commands": 0},
  "tool_intensive_queries": 25,
  "orchestrated_queries": 10,
  "estimated_savings": 12.50,
  "delegation_savings": 2.50,
  "sessions": [
    {
      "date": "2026-01-03",
      "queries": 25,
      "routes": {"fast": 8, "standard": 12, "deep": 2, "orchestrated": 3},
      "savings": 3.20
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

💰 Cost Savings (estimated)
───────────────────────────────────────────────────
Estimated Savings:   $12.50  (compared to always using Opus)
Delegation Savings:  $2.50   (from hybrid delegation)
Total Savings:       $15.00

⚠ Estimate basis: fixed avg of 1000 input / 2000 output tokens per
   query. These are rough estimates, not measurements from real
   Anthropic API usage records. Use for directional signal only.

📅 Today (2026-01-03)
───────────────────────────────────────────────────
Queries: 25
Savings: $3.20

Route Distribution:
  Fast: 8 | Standard: 12 | Deep: 2 | Orchestrated: 3
```

## Steps

1. Use the Read tool to read `~/.claude/router-stats.json`
2. If the file doesn't exist, inform the user that no stats are available yet
3. Calculate percentages for route distribution
4. Display exception counts if present (router_meta queries are handled by Opus despite classification)
5. Format and display the statistics
6. Include the savings comparison explanation

## Notes

- Savings are calculated assuming Opus would have been used for all queries
- Cost estimates use: Haiku 4.5 $1/$5, Sonnet 4.5 $3/$15, Opus 4.5 $5/$25 per 1M tokens
- **Savings are estimates, not measurements.** The per-query token count is a fixed average (1K input / 2K output) baked into the hook at write time — real token usage varies significantly by prompt. Read the `assumptions` field of `router-stats.json` for the exact constants, and surface that caveat whenever you quote the savings number to the user.
- **Exceptions**: Queries about the router itself are classified but handled by Opus (per CLAUDE.md rules). This is intentional - users discussing the router get the most capable model while still seeing what the classifier decided.
