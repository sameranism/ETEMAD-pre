"""P16 internal assistant: versioned, explicitly allowlisted newsroom tools.

The registry exposes no arbitrary URLs, studio tools or external side effects.
Execution must use the existing authenticated application handlers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.modules.intelligence.retrieval import normalized

AssistantAction = Literal["research", "analysis", "public_report"]
AssistantRequestAction = Literal["auto", "research", "analysis", "public_report"]


@dataclass(frozen=True)
class AssistantTool:
    action: AssistantAction
    grant: str
    label: str
    description: str


TOOLS = (
    AssistantTool("research", "chat", "پژوهش خبری", "پاسخ مستند از آرشیو مجاز"),
    AssistantTool("analysis", "run", "تحلیل رویدادها", "تحلیل توصیفی و خط زمانی"),
    AssistantTool("public_report", "report", "تولید گزارش", "پیش‌نویس مستند گزارش عمومی"),
)


def identity() -> dict[str, str]:
    """Product identity, not unverified claims about the newspaper's history."""
    return {
        "name": "ETEMAD AI Assistant",
        "name_fa": "دستیار هوشمند اعتماد",
        "audience": "internal_newsroom",
        "studio_audience": "public_subscription",
        "mode": "bounded_tool_router",
    }

def select_action(action: AssistantRequestAction, question: str) -> AssistantAction:
    """Conservative local intent selection over user text, never source text.

    Automatic mode can only select read-only research or analysis; reports
    require an explicit slot and publishing/Studio are not tools here.
    Current grants are checked by the selected handler, without fallback.
    """
    if action != "auto":
        return action
    query = normalized(question)
    if any(phrase in query for phrase in (
        "تحلیل", "مقایسه", "خط زمانی", "تفاوت پوشش", "روند پوشش",
        "compare", "analyze", "analyse", "timeline",
    )):
        return "analysis"
    return "research"
