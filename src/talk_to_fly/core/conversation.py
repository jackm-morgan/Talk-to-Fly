"""Conversation-state utilities for clarification and replanning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time


@dataclass
class ConversationTurn:
    role: str
    content: str
    kind: str = "message"
    timestamp: float = field(default_factory=time.time)


@dataclass
class PendingClarification:
    mission: Any
    question: str
    created_at: float = field(default_factory=time.time)


class ConversationMemory:
    """Lightweight interactive session memory.

    Tracks recent user/assistant turns and any unresolved clarification question
    so short follow-up answers (for example `5`) can be interpreted in context.
    Only the most recent user/assistant messages are injected into planner prompts.
    """

    def __init__(self, *, max_turns: int = 10):
        self.max_turns = max(1, int(max_turns))
        self.turns: List[ConversationTurn] = []
        self.pending: Optional[PendingClarification] = None

    def _trim(self) -> None:
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns :]

    def add_user(self, content: str, *, kind: str = "user_input") -> None:
        self.turns.append(ConversationTurn(role="user", content=str(content), kind=kind))
        self._trim()

    def add_assistant(self, content: str, *, kind: str = "assistant_output") -> None:
        self.turns.append(ConversationTurn(role="assistant", content=str(content), kind=kind))
        self._trim()

    def set_pending_clarification(self, mission: Any, question: str) -> None:
        self.pending = PendingClarification(mission=mission, question=question)
        self.add_assistant(question, kind="clarification_question")

    def clear_pending_clarification(self) -> None:
        self.pending = None

    def has_pending_clarification(self) -> bool:
        return self.pending is not None

    def build_prompt_history(self, *, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """Return the last user/assistant messages for prompt injection.

        The planner prompt should see only actual messages to/from the LLM, not
        internal system notes. Pending clarification questions are already stored
        as assistant turns by set_pending_clarification().
        """
        max_items = self.max_turns if limit is None else max(1, int(limit))
        items: List[Dict[str, str]] = []
        for turn in self.turns:
            role = str(turn.role).strip().lower()
            if role not in {"user", "assistant"}:
                continue
            items.append(
                {
                    "role": role,
                    "kind": turn.kind,
                    "content": turn.content,
                }
            )
        return items[-max_items:]

    def build_clarification_request(self, user_answer: str) -> tuple[str, Any]:
        if self.pending is None:
            raise RuntimeError("No pending clarification to resolve.")

        mission = self.pending.mission
        question = self.pending.question
        original = getattr(mission, "objective", "") or getattr(mission, "user_task", "")
        request = (
            f"Original mission: {original}\n"
            f"Previous clarification question: {question}\n"
            f"User clarification answer: {user_answer}\n\n"
            "Interpret the user's answer only in the context of that unresolved question. "
            "Continue the same mission from the CURRENT drone state. "
            "Return only the DSL plan still needed now. "
            "Do not ask the user to restate the whole task unless the answer is still genuinely ambiguous."
        )
        return request, mission
