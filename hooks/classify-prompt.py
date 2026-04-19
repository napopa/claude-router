#!/usr/bin/env python3
"""
Claude Router - UserPromptSubmit Hook
Classifies prompts using hybrid approach:
1. Rule-based patterns (instant, free)
2. Haiku LLM fallback for low-confidence cases (~$0.001)

Cache strategy: file-cache only; per-process in-memory cache removed in v2.1.0
(the hook runs once per prompt so process-local dicts never survived
between invocations).

Part of claude-router: https://github.com/napopa/claude-router
"""
from __future__ import annotations
import json
import sys
import os
import re
import hashlib
from pathlib import Path
from datetime import datetime
# Cross-platform file locking
import platform
if platform.system() == "Windows":
    import msvcrt
    def lock_file(f, exclusive=False):
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if exclusive else msvcrt.LK_LOCK, 1)
    def unlock_file(f):
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl
    def lock_file(f, exclusive=False):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    def unlock_file(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, content: str) -> None:
    """Atomic write: temp file + os.replace().

    Avoids the truncate-before-lock race where another reader could see
    an empty file in the window between open('w') (which truncates) and
    flock() acquisition. On POSIX os.replace() is atomic; concurrent
    writers degrade to last-writer-wins instead of file corruption.
    """
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w') as f:
        f.write(content)
    os.replace(tmp, path)


# Confidence threshold for LLM fallback
CONFIDENCE_THRESHOLD = 0.7

# Stats file location
STATS_FILE = Path.home() / ".claude" / "router-stats.json"


# Exception patterns - queries that will be handled by Opus despite classification
# (router meta-questions, slash commands handled in main())
# Pre-compiled for performance
EXCEPTION_PATTERNS = [
    re.compile(r'\brouter\b.*\b(stats?|config|setting|work)'),
    re.compile(r'\brouting\b'),
    re.compile(r'claude.?router'),
    re.compile(r'\bexception\b.*\b(track|detect)'),
    re.compile(r'\bclassif(y|ication)\b.*\b(prompt|query)'),
    # Widened in v2.1.0: natural phrasings for router meta-questions
    re.compile(r'\b(audit|review|fix|debug)\b.{0,40}\brouter\b'),
    re.compile(r'\b(what|how|why)\b.{0,30}\brouter\b.{0,30}\b(do|does|work|say)\b'),
    re.compile(r'\brouter\b.{0,30}\b(explain|describe|docs)\b'),
]

# Classification cache settings
CACHE_MAX_ENTRIES = 100
CACHE_TTL_DAYS = 30

# Prompt length cutoffs (used by classify_by_rules() no-pattern fallback).
# Very short prompts are almost always true lookups; short prompts are
# usually real work and get promoted to standard so the LLM fallback can
# correct them; long prompts always get LLM fallback when a key is present.
VERY_SHORT_CHARS = 60
SHORT_PROMPT_CHARS = 200
# For longer prompts we still run the LLM fallback, but the middle of a
# 10KB stack trace adds no classification signal. Head+tail is enough.
LLM_HEAD_CHARS = 500
LLM_TAIL_CHARS = 200

# Session state file for multi-turn context awareness
SESSION_STATE_FILE = Path.home() / ".claude" / "router-session.json"

# Follow-up query patterns (pre-compiled)
FOLLOW_UP_PATTERNS = [
    re.compile(r"^(and |also |now |next |then |but )"),
    re.compile(r"^(what about|how about|can you also|could you also)"),
    re.compile(r"^(yes|no|ok|okay|sure|right|great|perfect|thanks)[,.]?\s"),
    re.compile(r"^(do that|go ahead|proceed|continue|keep going)"),
    re.compile(r"^(actually|wait|instead|rather)"),
]

# Official plugins that claude-router can integrate with (optional)
SUPPORTED_PLUGINS = ["hookify", "ralph-loop", "code-review", "feature-dev"]


def detect_installed_plugins() -> dict:
    """Check which official plugins are installed."""
    detected = {}
    # Check common plugin locations
    plugin_locations = [
        Path.home() / ".claude" / "plugins",
        Path.home() / ".config" / "claude-code" / "plugins",
    ]
    for plugin in SUPPORTED_PLUGINS:
        detected[plugin] = False
        for loc in plugin_locations:
            if (loc / plugin).exists() or (loc / f"{plugin}.md").exists():
                detected[plugin] = True
                break
    return detected


def get_plugin_integrations() -> dict:
    """Get plugin integration states from knowledge state."""
    state = get_learning_state()
    return state.get("plugin_integrations", {
        plugin: {"enabled": False, "detected": False}
        for plugin in SUPPORTED_PLUGINS
    })


def is_plugin_enabled(plugin_name: str) -> bool:
    """Check if a plugin integration is both detected and enabled."""
    integrations = get_plugin_integrations()
    plugin = integrations.get(plugin_name, {})
    detected = detect_installed_plugins().get(plugin_name, False)
    enabled = plugin.get("enabled", False)
    return detected and enabled


def get_session_state() -> dict:
    """Get the current session state for multi-turn context awareness."""
    try:
        if SESSION_STATE_FILE.exists():
            with open(SESSION_STATE_FILE, 'r') as f:
                state = json.load(f)
                # Check if session is stale (older than 30 minutes)
                last_query = state.get("last_query_time", 0)
                if datetime.now().timestamp() - last_query > 1800:  # 30 min
                    return {"last_route": None, "conversation_depth": 0}
                return state
        return {"last_route": None, "conversation_depth": 0}
    except Exception:
        return {"last_route": None, "conversation_depth": 0}


def update_session_state(route: str, metadata: dict = None, state: dict = None):
    """Update session state after a routing decision.

    If ``state`` is provided, reuse it instead of re-reading from disk.
    classify_hybrid() already loads session state for context-boost logic,
    so threading it through here collapses two disk reads into one per hook
    invocation.
    """
    try:
        SESSION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if state is None:
            state = get_session_state()
        state["last_route"] = route
        state["last_query_time"] = datetime.now().timestamp()
        state["conversation_depth"] = state.get("conversation_depth", 0) + 1
        state["last_metadata"] = metadata or {}
        with open(SESSION_STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception:
        pass  # Don't fail on state errors


def is_follow_up_query(prompt: str) -> bool:
    """Check if the query appears to be a follow-up to a previous query."""
    prompt_lower = prompt.lower().strip()
    for pattern in FOLLOW_UP_PATTERNS:
        if pattern.match(prompt_lower):
            return True
    return False


def apply_context_boost(result: dict, session_state: dict, is_follow_up: bool) -> dict:
    """Apply a route/confidence adjustment based on conversation context.

    v2.1.0: when the last route was standard/deep and the current
    classification is fast, actually promote to standard (not merely
    bump confidence inside fast). Confidence stays under 0.7 so the
    LLM fallback can still correct if it disagrees.
    """
    if not is_follow_up:
        return result

    last_route = session_state.get("last_route")
    if not last_route:
        return result

    result["metadata"] = result.get("metadata", {})
    result["metadata"]["follow_up"] = True

    if last_route in ("deep", "standard") and result["route"] == "fast":
        current_confidence = result.get("confidence", 0.5)
        result["route"] = "standard"
        result["confidence"] = min(0.65, max(current_confidence, 0.6))
        result["metadata"]["context_boost"] = f"follow_up_to_{last_route}"

    return result


def get_knowledge_dir() -> Path:
    """Get the knowledge directory path (project-local).

    Only looks at knowledge/ relative to this script's location.
    The prior Path.cwd() fallback was removed in v2.1.0 — it let a
    hostile `knowledge/` directory in the user's cwd hijack knowledge
    lookups if Claude Code was launched from that directory.
    """
    script_dir = Path(__file__).parent.parent  # Go up from hooks/ to project root
    knowledge_dir = script_dir / "knowledge"
    if knowledge_dir.exists():
        return knowledge_dir
    return None

def generate_fingerprint(prompt: str) -> str:
    """Generate a fingerprint for a prompt to enable fuzzy cache matching."""
    # Normalize: lowercase, strip, collapse whitespace
    normalized = re.sub(r'\s+', ' ', prompt.lower().strip())

    # Extract key terms (remove common words)
    stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                  'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
                  'would', 'could', 'should', 'may', 'might', 'must', 'can',
                  'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
                  'this', 'that', 'these', 'those', 'it', 'its', 'i', 'me', 'my'}

    words = re.findall(r'\b[a-z]+\b', normalized)
    key_terms = [w for w in words if w not in stop_words and len(w) > 2]

    # Sort for consistency and take first 10 terms
    key_terms = sorted(set(key_terms))[:10]
    fingerprint_str = ' '.join(key_terms)

    # Generate hash
    return hashlib.md5(fingerprint_str.encode()).hexdigest()[:12]

def check_classification_cache(prompt: str) -> dict:
    """Check if a similar query exists in the file cache."""
    fingerprint = generate_fingerprint(prompt)

    try:
        knowledge_dir = get_knowledge_dir()
        if not knowledge_dir:
            return None

        cache_file = knowledge_dir / "cache" / "classifications.md"
        if not cache_file.exists():
            return None

        with open(cache_file, 'r') as f:
            content = f.read()

        # Look for matching fingerprint section.
        # `[\s\S]*?` + `\Z` gives unambiguous "up to next entry or end-of-string";
        # the prior `.*? + re.DOTALL + $` combo was pathological because `$`
        # matches at every `\n`, letting `.*?` collapse to zero characters.
        pattern = rf'## \[{re.escape(fingerprint)}\][\s\S]*?(?=\n## \[|\Z)'
        match = re.search(pattern, content)

        if not match:
            return None

        entry = match.group(0)

        # Parse the cached entry
        route_match = re.search(r'\*\*Route:\*\* (\w+)', entry)
        conf_match = re.search(r'\*\*Confidence:\*\* ([\d.]+)', entry)

        if route_match and conf_match:
            return {
                "route": route_match.group(1),
                "confidence": float(conf_match.group(1)),
                "signals": ["cache_hit"],
                "method": "cache",
                "metadata": {"cache_hit": True, "fingerprint": fingerprint}
            }

        return None
    except Exception:
        # Cache errors should never break classification
        return None

def write_classification_cache(prompt: str, result: dict):
    """Write a classification result to the file cache.

    Atomic via _atomic_write() — temp file + os.replace() — so concurrent
    writers never see a half-truncated file. Per-process in-memory cache
    was removed in v2.1.0 (hook runs once per prompt, so it never helped).
    """
    fingerprint = generate_fingerprint(prompt)

    try:
        knowledge_dir = get_knowledge_dir()
        if not knowledge_dir:
            return

        cache_file = knowledge_dir / "cache" / "classifications.md"
        if not cache_file.exists():
            return

        today = datetime.now().strftime("%Y-%m-%d")

        # Read existing cache
        with open(cache_file, 'r') as f:
            content = f.read()

        # Check if this fingerprint already exists
        if f'## [{fingerprint}]' in content:
            # Update last used date and hit count
            fp = re.escape(fingerprint)
            pattern = rf'(## \[{fp}\][\s\S]*?\*\*Last used:\*\* )\d{{4}}-\d{{2}}-\d{{2}}'
            content = re.sub(pattern, rf'\g<1>{today}', content)

            hit_pattern = rf'(## \[{fp}\][\s\S]*?\*\*Hit count:\*\* )(\d+)'
            hit_match = re.search(hit_pattern, content)
            if hit_match:
                new_count = int(hit_match.group(2)) + 1
                content = re.sub(hit_pattern, rf'\g<1>{new_count}', content)

            _atomic_write(cache_file, content)
            return

        # Create new entry
        # Truncate prompt for storage (first 50 chars + pattern type)
        prompt_preview = prompt[:50].replace('\n', ' ')
        if len(prompt) > 50:
            prompt_preview += "..."

        entry = f"""
## [{fingerprint}]
- **Query pattern:** "{prompt_preview}"
- **Route:** {result["route"]}
- **Confidence:** {result["confidence"]:.2f}
- **Last used:** {today}
- **Hit count:** 1
"""

        # Count existing entries
        entry_count = len(re.findall(r'^## \[', content, re.MULTILINE))

        # If at max, evict the oldest entry by Last-used date. Use split()
        # rather than a .*? regex — the prior findall approach mishandled
        # trailing whitespace and entries missing a "Last used:" line.
        if entry_count >= CACHE_MAX_ENTRIES:
            sections = re.split(r'(?=^## \[)', content, flags=re.MULTILINE)
            preamble, entries = sections[0], sections[1:]
            if entries:
                def entry_date(e: str) -> str:
                    m = re.search(r'\*\*Last used:\*\*\s*(\d{4}-\d{2}-\d{2})', e)
                    return m.group(1) if m else "0000-00-00"
                entries.sort(key=entry_date)
                # Drop the oldest
                entries = entries[1:]
                content = preamble + ''.join(entries)

        # Append new entry
        content = content.rstrip() + '\n' + entry

        # Update frontmatter entry count
        new_count = len(re.findall(r'^## \[', content, re.MULTILINE))
        content = re.sub(r'entry_count: \d+', f'entry_count: {new_count}', content)
        content = re.sub(r'last_updated: .*', f'last_updated: "{datetime.now().isoformat()}"', content)

        _atomic_write(cache_file, content)

    except Exception:
        # Cache errors should never break classification
        pass

def get_learning_state() -> dict:
    """Get the current learning state."""
    try:
        knowledge_dir = get_knowledge_dir()
        if not knowledge_dir:
            return {}
        state_file = knowledge_dir / "state.json"
        if state_file.exists():
            with open(state_file, 'r') as f:
                return json.load(f)
        return {}
    except Exception:
        return {}

def extract_learning_keywords() -> dict:
    """Extract keywords from learnings files to inform routing.

    (Process-local mtime cache removed in v2.1.0 — the hook runs once per
    prompt so it could not persist across calls.)
    """
    try:
        knowledge_dir = get_knowledge_dir()
        if not knowledge_dir:
            return {"deep_keywords": set(), "fast_keywords": set()}

        quirks_file = knowledge_dir / "learnings" / "quirks.md"
        patterns_file = knowledge_dir / "learnings" / "patterns.md"

        deep_keywords = set()
        fast_keywords = set()

        # Parse quirks.md for complexity indicators
        if quirks_file.exists():
            with open(quirks_file, 'r') as f:
                content = f.read().lower()
            # Extract keywords from quirk entries that suggest complexity
            for match in re.findall(r'## quirk:.*?(?=## |$)', content, re.DOTALL):
                if any(word in match for word in ['complex', 'tricky', 'careful', 'unusual', 'non-standard']):
                    # Extract the topic area (location field)
                    loc_match = re.search(r'\*\*location:\*\*\s*([^\n]+)', match)
                    if loc_match:
                        # Extract meaningful words from location
                        words = re.findall(r'\b[a-z]{3,}\b', loc_match.group(1).lower())
                        deep_keywords.update(words)

        # Parse patterns.md for simple patterns
        if patterns_file.exists():
            with open(patterns_file, 'r') as f:
                content = f.read().lower()
            for match in re.findall(r'## pattern:.*?(?=## |$)', content, re.DOTALL):
                if any(word in match for word in ['simple', 'straightforward', 'always', 'standard']):
                    # Extract topic keywords
                    insight_match = re.search(r'\*\*insight:\*\*\s*([^\n]+)', match)
                    if insight_match:
                        words = re.findall(r'\b[a-z]{3,}\b', insight_match.group(1).lower())
                        fast_keywords.update(words)

        return {"deep_keywords": deep_keywords, "fast_keywords": fast_keywords}
    except Exception:
        return {"deep_keywords": set(), "fast_keywords": set()}

def apply_learned_adjustments(prompt: str, result: dict) -> dict:
    """Apply learned knowledge to adjust routing confidence (conservative)."""
    try:
        state = get_learning_state()

        # Check if informed routing is enabled
        if not state.get("informed_routing", False):
            return result

        boost = state.get("informed_routing_boost", 0.1)
        keywords = extract_learning_keywords()

        prompt_lower = prompt.lower()
        deep_matches = sum(1 for kw in keywords["deep_keywords"] if kw in prompt_lower)
        fast_matches = sum(1 for kw in keywords["fast_keywords"] if kw in prompt_lower)

        # Only adjust if we have meaningful signal (2+ keyword matches)
        # Conservative: require more evidence for expensive routes
        if deep_matches >= 2 and result["route"] != "deep":
            # Boost toward deep, but cap at 0.1 increase
            result["confidence"] = min(1.0, result["confidence"] + boost)
            if result["confidence"] >= 0.8:
                result["route"] = "deep"
                result["metadata"] = result.get("metadata", {})
                result["metadata"]["learned_boost"] = "deep"

        elif fast_matches >= 2 and result["route"] == "deep":
            # If learned patterns suggest simple, consider downgrading
            # But be conservative - don't downgrade high-confidence deep
            if result["confidence"] < 0.8:
                result["route"] = "standard"
                result["metadata"] = result.get("metadata", {})
                result["metadata"]["learned_boost"] = "downgrade"

        return result
    except Exception:
        return result

def is_exception_query(prompt: str) -> tuple[bool, str]:
    """Check if query matches exception patterns that bypass routing."""
    prompt_lower = prompt.lower()
    for pattern in EXCEPTION_PATTERNS:
        if pattern.search(prompt_lower):  # Pre-compiled patterns use .search()
            return True, "router_meta"
    return False, None

# Classification patterns - Pre-compiled for performance
PATTERNS = {
    "fast": [
        # Simple questions
        re.compile(r"^what (is|are|does) "),
        re.compile(r"^how (do|does|to) "),
        re.compile(r"^(show|list|get) .{0,30}$"),
        # Formatting
        re.compile(r"\b(format|lint|prettify|beautify)\b"),
        # Git simple ops
        re.compile(r"\bgit (status|log|diff|add|commit|push|pull)\b"),
        # JSON/YAML
        re.compile(r"\b(json|yaml|yml)\b.{0,20}$"),
        # Regex
        re.compile(r"\bregex\b"),
        # Syntax questions
        re.compile(r"\bsyntax (for|of)\b"),
        re.compile(r"^(what|how).{0,50}\?$"),
    ],
    "sonnet": [
        # Bug fixes
        re.compile(r"\b(fix|debug|resolve)\b.{0,40}\b(bug|issue|error|problem|failure)\b"),
        # Feature/function implementation
        re.compile(r"\b(implement|build|create|add)\b.{0,40}\b(feature|function|method|class|endpoint|component|decorator|module|utility|helper|script|handler|hook|middleware)\b"),
        # Test writing
        re.compile(r"\b(write|add|create)\b.{0,30}\btests?\b"),
        # Local refactor (not broad refactors — those are deep)
        re.compile(r"\brefactor\b(?!.{0,30}(codebase|project|entire|across))"),
        # Code review / improvement
        re.compile(r"\b(review|improve|clean up)\b.{0,30}\bcode\b"),
        # Small file-level modifications
        re.compile(r"\b(update|modify|change)\b.{0,40}\b(function|method|class|file)\b"),
        # Serialization / validation
        re.compile(r"\b(parse|serialize|deserialize|validate|sanitize)\b"),
        # Error/exception handling
        re.compile(r"\b(handle|catch|raise|throw)\b.{0,30}\b(error|exception|edge case)\b"),
        # Local refactors
        re.compile(r"\b(rename|extract|inline)\b.{0,30}\b(variable|function|method)\b"),
    ],
    "deep": [
        # Architecture
        re.compile(r"\b(architect|architecture|design pattern|system design)\b"),
        # Subject-verb-inverted design ("design a multi-tenant auth system")
        re.compile(r"\bdesign\b.{0,40}\b(system|service|platform|pipeline|architecture)\b"),
        re.compile(r"\bscalable?\b"),
        # Security
        re.compile(r"\b(security|vulnerab|audit|penetration|exploit)\b"),
        # Multi-file
        re.compile(r"\b(across|multiple|all) (files?|components?|modules?)\b"),
        re.compile(r"\brefactor.{0,20}(codebase|project|entire)\b"),
        # Trade-offs
        re.compile(r"\b(trade-?off|compare|pros? (and|&) cons?)\b"),
        re.compile(r"\b(analyze|evaluate|assess).{0,30}(option|approach|strateg)\b"),
        # Complex
        re.compile(r"\b(complex|intricate|sophisticated)\b"),
        re.compile(r"\boptimiz(e|ation).{0,20}(performance|speed|memory)\b"),
        # Planning
        re.compile(r"\b(multi-?phase|extraction|standalone repo|migration)\b"),
    ],
    "tool_intensive": [
        # Codebase exploration
        re.compile(r"\b(find|search|locate) (all|every|each)"),
        re.compile(r"\bacross (the )?(codebase|project|repo)"),
        re.compile(r"\b(all|every) (file|instance|usage|reference)"),
        re.compile(r"\bwhere is .+ (used|called|defined)"),
        re.compile(r"\b(scan|explore|traverse) (the )?(codebase|project)"),
        # Multi-file modifications
        re.compile(r"\b(update|change|modify|rename|replace) .{0,20}(all|every|multiple) files?"),
        re.compile(r"\bglobal (search|replace|rename)"),
        re.compile(r"\brefactor.{0,30}(across|throughout|entire)"),
        # Build/test execution
        re.compile(r"\brun (all |the )?(tests?|specs?|suite)"),
        re.compile(r"\bbuild (the )?(project|app)"),
        re.compile(r"\bnpm (install|build|run)|yarn (install|build)|pip install"),
        # Dependency analysis
        re.compile(r"\b(dependency|import) (tree|graph|analysis)"),
        re.compile(r"\bwhat (depends on|imports|uses)"),
    ],
    "orchestration": [
        # Multi-step workflows
        re.compile(r"\b(step by step|sequentially|in order)\b"),
        re.compile(r"\bfor each (file|component|module)\b"),
        re.compile(r"\bacross the (entire|whole) (codebase|project)"),
        # Explicit multi-task
        re.compile(r"\band (also|then)\b.{0,50}\band (also|then)\b"),
        re.compile(r"\b(multiple|several|many) (tasks?|steps?|operations?)\b"),
    ],
}


def get_api_key():
    """Get API key from environment or common locations."""
    # Try environment first
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key

    # Try common .env locations
    search_paths = [
        Path.cwd() / ".env",                           # Current directory
        Path.cwd() / "server" / ".env",                # Server subdirectory
        Path.home() / ".anthropic" / "api_key",        # Anthropic config
        Path.home() / ".config" / "anthropic" / "key", # XDG config
    ]

    for env_path in search_paths:
        try:
            with open(env_path, "r") as f:
                content = f.read()
                # Handle both KEY=value and plain value formats
                for line in content.split("\n"):
                    if line.startswith("ANTHROPIC_API_KEY="):
                        return line.strip().split("=", 1)[1].strip('"\'')
                # If file is just the key (no assignment)
                if content.strip().startswith("sk-ant-"):
                    return content.strip()
        except (FileNotFoundError, PermissionError):
            continue

    return None


def log_routing_decision(route: str, confidence: float, method: str, signals: list, metadata: dict = None):
    """Log routing decision to stats file with optional metadata tracking.

    Only records factual data: route counts and query metadata.
    Savings are NOT accumulated here — they are computed on-the-fly
    by the stats display from the route distribution.
    """
    try:
        # Ensure directory exists
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Load existing stats or create new (v1.3 schema — no accumulated savings)
        stats = {
            "version": "1.3",
            "total_queries": 0,
            "routes": {"fast": 0, "standard": 0, "deep": 0, "orchestrated": 0},
            "exceptions": {"router_meta": 0, "slash_commands": 0},
            "tool_intensive_queries": 0,
            "orchestrated_queries": 0,
            "sessions": [],
            "last_updated": None
        }

        if STATS_FILE.exists():
            try:
                with open(STATS_FILE, "r") as f:
                    lock_file(f, exclusive=False)
                    stats = json.load(f)
                    unlock_file(f)
            except (json.JSONDecodeError, IOError):
                pass

        # Migrate: drop stale savings fields from older schemas
        stats.pop("estimated_savings", None)
        stats.pop("delegation_savings", None)
        stats.pop("assumptions", None)

        # Ensure v1.3 schema fields exist
        stats["version"] = "1.3"
        stats.setdefault("routes", {}).setdefault("orchestrated", 0)
        stats.setdefault("exceptions", {"router_meta": 0, "slash_commands": 0})
        stats.setdefault("tool_intensive_queries", 0)
        stats.setdefault("orchestrated_queries", 0)

        # Update stats
        stats["total_queries"] += 1
        metadata = metadata or {}

        # Track exceptions (queries that bypass routing due to CLAUDE.md rules)
        exception_type = metadata.get("exception_type")
        if exception_type:
            stats["exceptions"][exception_type] = stats["exceptions"].get(exception_type, 0) + 1

        # Track orchestrated vs regular routes
        if metadata.get("orchestration") and route == "deep":
            stats["routes"]["orchestrated"] += 1
            stats["orchestrated_queries"] += 1
        else:
            stats["routes"][route] += 1

        # Track tool-intensive queries
        if metadata.get("tool_intensive"):
            stats["tool_intensive_queries"] += 1

        # Get or create today's session
        today = datetime.now().strftime("%Y-%m-%d")
        session = None
        for s in stats.get("sessions", []):
            if s["date"] == today:
                session = s
                break

        if not session:
            session = {
                "date": today,
                "queries": 0,
                "routes": {"fast": 0, "standard": 0, "deep": 0}
            }
            stats.setdefault("sessions", []).append(session)

        session["queries"] += 1
        session["routes"][route] += 1
        session.pop("savings", None)

        # Keep only last 30 days of sessions
        stats["sessions"] = sorted(stats["sessions"], key=lambda x: x["date"], reverse=True)[:30]

        stats["last_updated"] = datetime.now().isoformat()

        # Atomic write: temp + os.replace() avoids the truncate-before-lock
        # race where readers could see an empty file during open('w').
        # Concurrent writers degrade to last-writer-wins — no corruption.
        _atomic_write(STATS_FILE, json.dumps(stats, indent=2))

    except Exception:
        # Don't fail the hook if stats logging fails
        pass


def classify_by_rules(prompt: str) -> dict:
    """
    Classify prompt using pre-compiled regex patterns.
    Returns route, confidence, signals, and optional metadata.

    Priority order:
    1. deep + orchestration (opus-orchestrator)
    2. deep + tool-intensive (deep-executor)
    3. deep alone (deep-executor)
    4. tool_intensive alone (standard-executor)
    5. orchestration alone (standard-executor)
    6. sonnet patterns (standard-executor — bulk of real coding work)
    7. fast patterns (fast-executor — trivial lookups)
    8. No-pattern fallback (very short → fast; otherwise → standard)

    Optimized with early exit when sufficient signals are found.
    """
    prompt_lower = prompt.lower()
    deep_signals = []
    tool_signals = []
    orch_signals = []
    sonnet_signals = []

    # Check for deep patterns first (highest priority)
    # Pre-compiled patterns use .search() method directly
    for pattern in PATTERNS["deep"]:
        match = pattern.search(prompt_lower)
        if match:
            deep_signals.append(match.group(0))
            # Early exit: if we have 3+ deep signals, no need to check more
            if len(deep_signals) >= 3:
                break

    # Check for tool-intensive patterns
    for pattern in PATTERNS.get("tool_intensive", []):
        match = pattern.search(prompt_lower)
        if match:
            tool_signals.append(match.group(0))
            # Early exit: if we have deep + tool signals, we have enough
            if deep_signals and len(tool_signals) >= 2:
                break

    # Check for orchestration patterns
    for pattern in PATTERNS.get("orchestration", []):
        match = pattern.search(prompt_lower)
        if match:
            orch_signals.append(match.group(0))
            # Early exit: if we have deep + orchestration, we have enough
            if deep_signals:
                break

    # Check for sonnet patterns (real coding work: bug fixes, features, tests)
    for pattern in PATTERNS.get("sonnet", []):
        match = pattern.search(prompt_lower)
        if match:
            sonnet_signals.append(match.group(0))
            if len(sonnet_signals) >= 3:
                break

    # Decision matrix. Orchestration-to-opus-orchestrator is narrow: we only
    # trigger it when a deep signal co-occurs with an *orchestration* signal
    # (multi-step / for-each / explicit multi-task). deep+tool alone routes to
    # deep-executor without the orchestration flag — tool-intensity is a
    # parallelism hint for workers, not a reason to spin up an Opus
    # orchestrator.
    if deep_signals and orch_signals:
        combined = deep_signals + orch_signals + tool_signals
        return {
            "route": "deep",
            "confidence": 0.95,
            "signals": combined[:4],
            "method": "rules",
            "metadata": {"orchestration": True, "tool_intensive": bool(tool_signals)}
        }

    if deep_signals and tool_signals:
        # Deep analysis over many files/tests — deep-executor handles it directly
        combined = deep_signals + tool_signals
        return {
            "route": "deep",
            "confidence": 0.9,
            "signals": combined[:4],
            "method": "rules",
            "metadata": {"tool_intensive": True}
        }

    # Deep wins over sonnet when both fire (architectural / security concerns
    # are higher-priority than the implementation signals that co-occur with them).
    if len(deep_signals) >= 2:
        return {"route": "deep", "confidence": 0.9, "signals": deep_signals[:3], "method": "rules"}

    if deep_signals:  # One deep signal
        return {"route": "deep", "confidence": 0.7, "signals": deep_signals, "method": "rules"}

    # Tool-intensive but not architecturally complex - route to standard
    if tool_signals:
        if len(tool_signals) >= 2:
            return {
                "route": "standard",
                "confidence": 0.85,
                "signals": tool_signals[:3],
                "method": "rules",
                "metadata": {"tool_intensive": True}
            }
        return {
            "route": "standard",
            "confidence": 0.7,
            "signals": tool_signals,
            "method": "rules",
            "metadata": {"tool_intensive": True}
        }

    # Orchestration alone (multi-step workflow) - route to standard
    if orch_signals:
        return {
            "route": "standard",
            "confidence": 0.75,
            "signals": orch_signals[:3],
            "method": "rules",
            "metadata": {"orchestration": True}
        }

    # Sonnet signals = real coding work (bug fixes, feature impl, tests,
    # local refactors). This is the bulk of what the router sees and it
    # should go to standard, not fast.
    if len(sonnet_signals) >= 2:
        return {"route": "standard", "confidence": 0.9, "signals": sonnet_signals[:3], "method": "rules"}

    if sonnet_signals:  # One sonnet signal
        return {"route": "standard", "confidence": 0.75, "signals": sonnet_signals, "method": "rules"}

    # Check for fast patterns
    fast_signals = []
    for pattern in PATTERNS["fast"]:
        match = pattern.search(prompt_lower)
        if match:
            fast_signals.append(match.group(0))
            if len(fast_signals) >= 2:
                return {"route": "fast", "confidence": 0.9, "signals": fast_signals[:3], "method": "rules"}

    if fast_signals:  # One fast signal
        return {"route": "fast", "confidence": 0.7, "signals": fast_signals, "method": "rules"}

    # No pattern matched. Reversed in v2.1.0 — default is now standard, not fast.
    # - Very short (<VERY_SHORT_CHARS): likely a true lookup, stay on fast above
    #   the LLM threshold so we don't pay Haiku for "list files".
    # - Short (VERY_SHORT..SHORT): likely real work; confidence below threshold
    #   so the LLM fallback gets a chance when a key is present.
    # - Long (>=SHORT_PROMPT_CHARS): clearly real work; LLM fallback if possible.
    if len(prompt) < VERY_SHORT_CHARS:
        return {
            "route": "fast",
            "confidence": 0.8,
            "signals": ["very short prompt, no patterns"],
            "method": "rules",
        }
    if len(prompt) < SHORT_PROMPT_CHARS:
        return {
            "route": "standard",
            "confidence": 0.6,
            "signals": ["short prompt, no patterns"],
            "method": "rules",
        }
    return {
        "route": "standard",
        "confidence": 0.55,
        "signals": ["no strong patterns"],
        "method": "rules",
    }


def truncate_for_llm(prompt: str) -> str:
    """Head + tail truncation for the LLM classifier.

    Haiku doesn't need the middle of a stack trace to decide a route.
    Caps a pasted 10KB blob at ~700 chars + a marker line.
    """
    if len(prompt) <= LLM_HEAD_CHARS + LLM_TAIL_CHARS:
        return prompt
    head = prompt[:LLM_HEAD_CHARS]
    tail = prompt[-LLM_TAIL_CHARS:]
    return f"{head}\n...[{len(prompt) - LLM_HEAD_CHARS - LLM_TAIL_CHARS} chars elided]...\n{tail}"


def classify_by_llm(prompt: str, api_key: str) -> dict:
    """
    Classify prompt using Haiku LLM.
    Used as fallback for low-confidence rule-based results.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return None

    client = Anthropic(api_key=api_key)

    truncated = truncate_for_llm(prompt)

    classification_prompt = f"""Classify this coding query. Most real coding work is "standard" — only pick "fast" for trivial lookups and "deep" for genuinely architectural work. Return ONLY valid JSON, no other text.

Query: "{truncated}"

Routes:
- "fast": status checks, syntax lookups, format/lint commands, single-line factual answers. If you'd answer in <50 words, it's fast.
- "standard" (DEFAULT): bug fixes, feature implementation, writing/modifying functions, code review, local refactoring, writing tests. Anything where you'd actually open a file and edit it.
- "deep": architecture/design decisions, security audits, multi-file refactors across an unfamiliar codebase, trade-off analysis with non-obvious answers, orchestration of >3 distinct steps.

Default to "standard" when uncertain.

Return JSON only:
{{"route": "fast|standard|deep", "confidence": 0.0-1.0, "signals": ["signal1", "signal2"], "tool_intensive": true|false}}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": classification_prompt}]
        )

        response_text = message.content[0].text.strip()

        # Handle potential markdown code blocks
        if "```" in response_text:
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:].strip()

        result = json.loads(response_text)
        result["method"] = "haiku-llm"
        return result

    except Exception as e:
        # Log error but don't fail
        print(f"LLM classification error: {e}", file=sys.stderr)
        return None


def classify_hybrid(prompt: str, session_state: dict = None) -> dict:
    """
    Hybrid classification: cache first, then rules, then LLM fallback,
    then learned adjustments, then context boost.

    ``session_state`` is passed in by main() so the hook reads
    router-session.json at most once per invocation.
    """
    # Step 0: Check cache for similar query (instant)
    cached = check_classification_cache(prompt)
    if cached:
        return cached

    # Step 1: Rule-based classification (instant, free)
    result = classify_by_rules(prompt)

    # Step 2: Check for multi-turn context (follow-up queries)
    if session_state is None:
        session_state = get_session_state()
    follow_up = is_follow_up_query(prompt)
    if follow_up:
        result = apply_context_boost(result, session_state, follow_up)

    # Step 3: If low confidence and API key available, use LLM
    if result["confidence"] < CONFIDENCE_THRESHOLD:
        api_key = get_api_key()
        if api_key:
            llm_result = classify_by_llm(prompt, api_key)
            if llm_result:
                # Apply learned adjustments (opt-in, conservative)
                llm_result = apply_learned_adjustments(prompt, llm_result)
                # Cache LLM result (more expensive to compute)
                write_classification_cache(prompt, llm_result)
                return llm_result

    # Step 4: Apply learned adjustments (opt-in, conservative)
    result = apply_learned_adjustments(prompt, result)

    # Step 5: Cache the result for future queries
    write_classification_cache(prompt, result)

    return result


def main():
    """Main hook handler."""
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    prompt = input_data.get("prompt", "")

    if not prompt or len(prompt) < 10:
        sys.exit(0)

    # Handle slash commands
    stripped = prompt.strip().lower()
    if stripped.startswith("/"):
        # Special handling for /route with explicit model
        if stripped.startswith("/route "):
            route_args = prompt.strip()[7:].strip()  # Get everything after "/route "
            first_word = route_args.split()[0].lower() if route_args.split() else ""

            # Check for explicit model specification
            model_map = {
                "opus": ("deep", "deep-executor", "Opus"),
                "deep": ("deep", "deep-executor", "Opus"),
                "sonnet": ("standard", "standard-executor", "Sonnet"),
                "standard": ("standard", "standard-executor", "Sonnet"),
                "haiku": ("fast", "fast-executor", "Haiku"),
                "fast": ("fast", "fast-executor", "Haiku"),
            }

            if first_word in model_map:
                route, subagent, model = model_map[first_word]
                query = " ".join(route_args.split()[1:])  # Rest after model

                context = f"""[Claude Router] EXPLICIT MODEL OVERRIDE
Route: {route} | Model: {model} | Source: User specified "{first_word}"

USER EXPLICITLY REQUESTED {model.upper()}. This is NOT a suggestion - it is a COMMAND.

CRITICAL: Spawn "claude-router:{subagent}" with the query below. DO NOT reclassify. DO NOT override.

Query: {query}

Example:
Task(subagent_type="claude-router:{subagent}", prompt="{query}", description="Route to {model}")"""

                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": context
                    }
                }
                print(json.dumps(output))
                sys.exit(0)

        # Special handling for /retry with explicit model
        if stripped.startswith("/retry "):
            retry_args = prompt.strip()[7:].strip().lower()  # Get everything after "/retry "

            retry_model_map = {
                "opus": ("deep", "deep-executor", "Opus"),
                "deep": ("deep", "deep-executor", "Opus"),
                "sonnet": ("standard", "standard-executor", "Sonnet"),
                "standard": ("standard", "standard-executor", "Sonnet"),
            }

            if retry_args in retry_model_map:
                route, subagent, model = retry_model_map[retry_args]

                context = f"""[Claude Router] EXPLICIT RETRY OVERRIDE
Route: {route} | Model: {model} | Source: User specified "/retry {retry_args}"

USER EXPLICITLY REQUESTED {model.upper()} FOR RETRY. This is NOT a suggestion - it is a COMMAND.

CRITICAL: Read the last query from session state (~/.claude/router-session.json) and spawn "claude-router:{subagent}".
DO NOT auto-escalate. DO NOT choose a different model. Use {model.upper()}.

Example:
Task(subagent_type="claude-router:{subagent}", prompt="<last query from session>", description="Retry with {model}")"""

                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": context
                    }
                }
                print(json.dumps(output))
                sys.exit(0)

        # Skip other slash commands (let skills handle them)
        sys.exit(0)

    # Check for exception queries (router meta-questions).
    # In v2.1.0, exception queries fully bypass the routing directive:
    # they still increment the router_meta counter, but do not emit the
    # Task() directive — the main agent answers the user directly.
    # Matches the documented behavior in CLAUDE.md.
    is_exception, exception_type = is_exception_query(prompt)
    if is_exception:
        log_routing_decision(
            route="fast",  # placeholder; 'route' is required but we count under exceptions
            confidence=1.0,
            method="exception",
            signals=["router_meta"],
            metadata={"exception_type": exception_type},
        )
        sys.exit(0)

    # Load session state once and thread it through classify + update below.
    session_state = get_session_state()

    # Classify using hybrid approach
    result = classify_hybrid(prompt, session_state=session_state)

    route = result["route"]
    confidence = result["confidence"]
    signals = result["signals"]
    method = result.get("method", "rules")

    # Get metadata for orchestration/tool-intensive routing
    metadata = result.get("metadata", {})

    # Log routing decision to stats
    log_routing_decision(route, confidence, method, signals, metadata)

    # Update session state for multi-turn context awareness
    # (reuses the state we already loaded — no second read)
    update_session_state(route, metadata, state=session_state)

    # Map route to subagent and model
    # Use opus-orchestrator for complex tasks with orchestration flag
    if route == "deep" and metadata.get("orchestration"):
        subagent = "opus-orchestrator"
        model = "Opus (Orchestrator)"
    else:
        subagent_map = {"fast": "fast-executor", "standard": "standard-executor", "deep": "deep-executor"}
        model_map = {"fast": "Haiku", "standard": "Sonnet", "deep": "Opus"}
        subagent = subagent_map[route]
        model = model_map[route]

    # Minimal, cache-stable directive: static prefix + one dynamic line.
    # Confidence/method/signals are diagnostic and already logged via
    # log_routing_decision(); omitting them from the directive keeps the
    # main agent's injected context byte-stable across routes of the same
    # class and shrinks the delegation turn to a single Task() echo.
    query_json = json.dumps(prompt)
    context = (
        "[Claude Router] ROUTING DIRECTIVE\n"
        "Delegate this turn — do not answer the user yourself.\n"
        "Invoke exactly one Task call, then stop:\n\n"
        f'Task(subagent_type="claude-router:{subagent}", '
        f'description="Route to {model}", prompt={query_json})'
    )

    # Output as JSON with hookSpecificOutput for proper injection
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
