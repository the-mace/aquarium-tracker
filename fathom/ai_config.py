"""Shared Anthropic model selection for all Fathom LLM call sites.

Policy (see CLAUDE.md "AI model strategy"):
- Single provider: Anthropic Claude only (no dual-provider maintenance).
- Mass-class Sonnet only — not Opus/Fable (overkill/price) and not Haiku (too light).
- Pin to the current Sonnet generation; bump this constant when a new Sonnet ships
  and is validated on analysis + chat.
"""

# Current mass-class model used for analysis, chat, import, reference info, etc.
CLAUDE_MODEL = "claude-sonnet-5"
