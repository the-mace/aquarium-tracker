"""Shared Anthropic model selection for all Fathom LLM call sites.

Policy (see CLAUDE.md "AI model strategy"):
- Single provider: Anthropic Claude only (no dual-provider maintenance).
- Mass-class Sonnet only — not Opus/Fable (overkill/price) and not Haiku (too light).
- Pin to the current Sonnet generation; bump this constant when a new Sonnet ships
  and is validated on analysis + chat.
"""

# Current mass-class model used for analysis, chat, import, reference info, etc.
CLAUDE_MODEL = "claude-sonnet-5"

# Sonnet 5 adaptive thinking is ON by default (omit the thinking field, or pass
# type=adaptive). Thinking tokens share max_tokens with the visible reply, so
# budgets below must leave room for both. If a call still returns only thinking
# (no TextBlock), call sites retry once with CLAUDE_THINKING_DISABLED.
CLAUDE_THINKING_DISABLED = {"type": "disabled"}

# Prefix for auto observations written when analysis fails — wait page and UI
# treat these as a terminal failure signal (not a normal analysis note).
ANALYSIS_FAILURE_PREFIX = "AI analysis failed:"

# Output budgets sized for adaptive thinking + reply (Sonnet 5).
# Analysis was 1536 pre-thinking; prod 2026-07-27 burned the whole budget on
# thinking and returned no text.
CLAUDE_MAX_TOKENS_ANALYSIS = 8192
CLAUDE_MAX_TOKENS_ISSUE_REVIEW = 2048
CLAUDE_MAX_TOKENS_SUMMARY = 4096
CLAUDE_MAX_TOKENS_NOTES_PROPOSAL = 2048
CLAUDE_MAX_TOKENS_RECOMMENDATION = 1500
