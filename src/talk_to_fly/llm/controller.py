"""Planner and plan-review helpers for the LLM interface."""

from __future__ import annotations

from dataclasses import dataclass
import json
from importlib.resources import files as _res_files
import os
import sys
import threading
import time
from typing import Callable, Optional, Tuple, Dict, Any, List

from dotenv import load_dotenv
from openai import OpenAI

from talk_to_fly.logging.logger import log_trace, log_verbose, log_status
from talk_to_fly.skillset import skillset_to_prompt_json


load_dotenv()

PLANNER_MODEL = os.getenv("TALK_TO_FLY_MODEL", "gpt-5.4")
PLANNER_STREAM_MODEL = os.getenv("TALK_TO_FLY_STREAM_MODEL", PLANNER_MODEL)
PROMPT_CACHE_KEY = os.getenv("TALK_TO_FLY_PROMPT_CACHE_KEY", "talk-to-fly:dsl:v1")
PROMPT_CACHE_RETENTION = os.getenv("TALK_TO_FLY_PROMPT_CACHE_RETENTION", "24h")

# Unique markers used to preserve the existing prompt structure while splitting
# the prompt into a cacheable static prefix and a dynamic suffix.
_TASK_MARKER = "<<<TALK_TO_FLY_TASK_DESCRIPTION>>>"
_HISTORY_MARKER = "<<<TALK_TO_FLY_EXECUTION_HISTORY>>>"
_STATUS_MARKER = "<<<TALK_TO_FLY_DRONE_STATUS>>>"
_CONVERSATION_MARKER = "<<<TALK_TO_FLY_CONVERSATION_HISTORY>>>"


@dataclass(frozen=True)
class PromptParts:
    static_prefix: str
    dynamic_suffix: str


@dataclass(frozen=True)
class PlanTimings:
    ttft_ms: Optional[float]
    total_ms: float
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    static_prompt_tokens: Optional[int] = None


@dataclass(frozen=True)
class PlanReview:
    requires_clarification: bool = False
    clarification_question: Optional[str] = None
    ambiguity_reasons: Tuple[str, ...] = ()
    safety_risks: Tuple[str, ...] = ()
    plan_issues: Tuple[str, ...] = ()
    suggested_revision_needed: bool = False
    revised_dsl: Optional[str] = None
    summary: str = ""
    confidence: Optional[float] = None

    def final_dsl(self, original: str) -> str:
        revised = (self.revised_dsl or "").strip()
        if self.suggested_revision_needed and revised:
            return revised
        return (original or "").strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requires_clarification": self.requires_clarification,
            "clarification_question": self.clarification_question,
            "ambiguity_reasons": list(self.ambiguity_reasons),
            "safety_risks": list(self.safety_risks),
            "plan_issues": list(self.plan_issues),
            "suggested_revision_needed": self.suggested_revision_needed,
            "revised_dsl": self.revised_dsl,
            "summary": self.summary,
            "confidence": self.confidence,
        }


def _spinner_task(stop_event):
    """Display a spinner in the terminal while stop_event is not set."""
    spinner = "|/-\\"
    idx = 0
    while not stop_event.is_set():
        sys.stdout.write(f"\r[LLM] Thinking... {spinner[idx % len(spinner)]}")
        sys.stdout.flush()
        idx += 1
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * 40 + "\r")


def _format_execution_history(history) -> str:
    try:
        if not history:
            return "[]"
        return json.dumps(history, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(history)


def _format_conversation_history(history, *, limit: int = 10) -> str:
    """Format the most recent user/assistant messages for prompt injection."""
    try:
        if not history:
            return "[]"

        items = []
        for item in history:
            if isinstance(item, dict):
                role = str(item.get("role", "")).strip().lower()
                if role not in {"user", "assistant"}:
                    continue
                content = item.get("content")
                if content is None:
                    content = item.get("text")
                if content is None:
                    content = str(item)
                out = {
                    "role": role,
                    "content": str(content),
                }
                kind = item.get("kind")
                if kind is not None:
                    out["kind"] = str(kind)
                items.append(out)
            else:
                items.append({
                    "role": "assistant",
                    "kind": "history_seed",
                    "content": str(item),
                })

        return json.dumps(items[-max(1, int(limit)):], ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(history)


def _stringify_status(status: Any) -> str:
    try:
        if isinstance(status, str):
            return status
        return json.dumps(status, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(status)


def _load_prompt_assets(drone) -> Dict[str, str]:
    assets = _res_files("talk_to_fly.assets")
    prompt_structure = assets.joinpath("prompt_structure.txt").read_text(encoding="utf-8")
    guidelines = assets.joinpath("guidelines.txt").read_text(encoding="utf-8")
    dsl_syntax = assets.joinpath("dsl_syntax.txt").read_text(encoding="utf-8")
    constraints = assets.joinpath("constraints.txt").read_text(encoding="utf-8")
    examples = assets.joinpath("examples.txt").read_text(encoding="utf-8")
    high, low = skillset_to_prompt_json(drone.skills)

    return {
        "prompt_structure": prompt_structure,
        "guidelines": guidelines,
        "high_level_skills": high,
        "low_level_skills": low,
        "dsl_syntax": dsl_syntax,
        "constraints": constraints,
        "examples": examples,
    }


def _create_prompt_parts(
    task_description: str,
    drone,
    *,
    execution_history=None,
    current_status=None,
    mission_context: Optional[str] = None,
    planning_mode: Optional[str] = None,
    conversation_history=None,
) -> PromptParts:
    assets = _load_prompt_assets(drone)

    templated_prompt = assets["prompt_structure"].format(
        guidelines=assets["guidelines"],
        high_level_skills=assets["high_level_skills"],
        low_level_skills=assets["low_level_skills"],
        dsl_syntax=assets["dsl_syntax"],
        constraints=assets["constraints"],
        examples=assets["examples"],
        task_description=_TASK_MARKER,
        execution_history=_HISTORY_MARKER,
        drone_status=_STATUS_MARKER,
        conversation_history=_CONVERSATION_MARKER,
    )

    marker_positions = [
        pos for pos in (
            templated_prompt.find(_TASK_MARKER),
            templated_prompt.find(_HISTORY_MARKER),
            templated_prompt.find(_STATUS_MARKER),
            templated_prompt.find(_CONVERSATION_MARKER),
        )
        if pos != -1
    ]

    if not marker_positions:
        # Fallback: preserve prior behaviour if the structure unexpectedly does not
        # include the dynamic placeholders.
        static_prefix = templated_prompt.strip()
        dynamic_suffix = ""
    else:
        first_dynamic_idx = min(marker_positions)
        static_prefix = templated_prompt[:first_dynamic_idx].rstrip()
        dynamic_suffix_template = templated_prompt[first_dynamic_idx:].lstrip()
        dynamic_suffix = (
            dynamic_suffix_template
            .replace(_TASK_MARKER, task_description)
            .replace(_HISTORY_MARKER, _format_execution_history(execution_history if execution_history is not None else getattr(drone, "hist", None)))
            .replace(_STATUS_MARKER, _stringify_status(current_status if current_status is not None else getattr(drone, "get_status_dict", drone.get_status)()))
            .replace(_CONVERSATION_MARKER, _format_conversation_history(conversation_history))
            .strip()
        )

        extra_sections = []
        if planning_mode == "clarification":
            extra_sections.append(
                "# Planning mode\nclarification\n"
                "The latest user input is a clarification answer to a previous question. "
                "Interpret short replies only in that context and continue the same mission from the current state. "
                "Do not treat the answer as a brand-new standalone task."
            )
        elif planning_mode and str(planning_mode).startswith("replan"):
            extra_sections.append(
                f"# Planning mode\n{planning_mode}\n"
                "You are recovering from an execution failure. Return only the recovery or remaining plan from the current state."
            )
        elif planning_mode:
            extra_sections.append(
                f"# Planning mode\n{planning_mode}\n"
                "This is a fresh mission request from the user. Plan from scratch unless history/state clearly show you are already mid-mission."
            )
        if mission_context:
            extra_sections.append(f"# Mission context\n{mission_context.strip()}")
        if extra_sections:
            dynamic_suffix = f"{dynamic_suffix}\n\n" + "\n\n".join(extra_sections)

    return PromptParts(static_prefix=static_prefix, dynamic_suffix=dynamic_suffix)


def _create_prompt(task_description: str, drone, **kwargs) -> str:
    parts = _create_prompt_parts(task_description, drone, **kwargs)
    if parts.dynamic_suffix:
        return f"{parts.static_prefix}\n\n{parts.dynamic_suffix}".strip()
    return parts.static_prefix


def _build_messages(task_description: str, drone, **kwargs) -> Tuple[list[dict[str, str]], PromptParts]:
    parts = _create_prompt_parts(task_description, drone, **kwargs)
    messages: list[dict[str, str]] = []

    if parts.static_prefix:
        messages.append({"role": "system", "content": parts.static_prefix})
    if parts.dynamic_suffix:
        messages.append({"role": "user", "content": parts.dynamic_suffix})
    elif not messages:
        messages.append({"role": "user", "content": ""})

    return messages, parts


def _extract_usage_fields(usage: Any) -> Dict[str, Optional[int]]:
    prompt_tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
    completion_tokens = getattr(usage, "completion_tokens", None) if usage is not None else None
    total_tokens = getattr(usage, "total_tokens", None) if usage is not None else None

    cached_tokens = None
    if usage is not None:
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details is None and isinstance(usage, dict):
            prompt_details = usage.get("prompt_tokens_details")
        if prompt_details is not None:
            cached_tokens = getattr(prompt_details, "cached_tokens", None)
            if cached_tokens is None and isinstance(prompt_details, dict):
                cached_tokens = prompt_details.get("cached_tokens")

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
    }


def _chat_completion_create(client: OpenAI, **kwargs):
    """Create a chat completion with graceful fallback if newer cache args are unsupported."""
    try:
        return client.chat.completions.create(**kwargs)
    except TypeError as exc:
        msg = str(exc)
        stripped = dict(kwargs)
        changed = False
        for key in ("prompt_cache_retention", "prompt_cache_key", "stream_options"):
            if key in stripped and key in msg:
                stripped.pop(key, None)
                changed = True
        if not changed:
            raise
        log_verbose(f"[LLM] Retrying without unsupported SDK args: {msg}")
        return client.chat.completions.create(**stripped)


def _count_messages_tokens_via_api(
    client: OpenAI,
    *,
    model: str,
    messages: list[dict[str, str]],
) -> int:
    """Return exact input-token count when the SDK supports the token counting API."""
    result = client.responses.input_tokens.count(model=model, input=messages)
    return int(getattr(result, "input_tokens"))


def _count_messages_tokens_locally(*, model: str, messages: list[dict[str, str]]) -> int:
    """Fallback local count for plain text messages.

    This is only an estimate. Use the API count method when available.
    """
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError(
            "Token counting requires either the OpenAI token-counting API or the tiktoken package."
        ) from exc

    try:
        enc = tiktoken.encoding_for_model(model)
    except Exception:
        enc = tiktoken.get_encoding("o200k_base")

    # Rough message wrapper overhead plus encoded text content.
    total = 0
    for message in messages:
        total += 4
        total += len(enc.encode(message.get("role", "")))
        total += len(enc.encode(message.get("content", "")))
    total += 2
    return total


def count_prompt_tokens(
    task_description: str,
    drone,
    *,
    model: str = PLANNER_MODEL,
    include_static_only: bool = False,
    execution_history=None,
    current_status=None,
    mission_context: Optional[str] = None,
    planning_mode: Optional[str] = None,
    conversation_history=None,
) -> int:
    """Count tokens for the planner prompt.

    - include_static_only=False: counts the exact messages sent to the planner.
    - include_static_only=True: counts only the cacheable static prefix message.

    Uses the OpenAI token-counting API when available; otherwise falls back to a
    local tiktoken estimate for plain text messages.
    """
    load_dotenv()
    apikey = os.getenv("OPENAI_API_KEY")
    if not apikey:
        raise ValueError("OPENAI_API_KEY not set in environment.")

    client = OpenAI(api_key=apikey)
    messages, parts = _build_messages(
        task_description,
        drone,
        execution_history=execution_history,
        current_status=current_status,
        mission_context=mission_context,
        planning_mode=planning_mode,
        conversation_history=conversation_history,
    )
    if include_static_only:
        messages = [{"role": "system", "content": parts.static_prefix}] if parts.static_prefix else []

    if not messages:
        return 0

    try:
        return _count_messages_tokens_via_api(client, model=model, messages=messages)
    except Exception as exc:
        log_verbose(f"[LLM] Falling back to local token estimate: {exc}")
        return _count_messages_tokens_locally(model=model, messages=messages)


def count_static_prompt_tokens(drone, *, model: str = PLANNER_MODEL) -> int:
    """Convenience helper for the cacheable static planner prefix only."""
    return count_prompt_tokens("", drone, model=model, include_static_only=True)


def plan_dsl(
    task_description: str,
    drone,
    *,
    stream: bool = True,
    show_spinner: bool = False,
    print_plan: bool = False,
    on_token: Optional[Callable[[str], None]] = None,
    submitted_at_s: Optional[float] = None,
    execution_history=None,
    current_status=None,
    mission_context: Optional[str] = None,
    planning_mode: Optional[str] = None,
    conversation_history=None,
) -> Tuple[str, PlanTimings]:
    """Return ``(plan, timings)``.

    - If stream=True, attempts streaming responses and measures TTFT (time-to-first-token).
    - If streaming is unavailable/fails, falls back to non-stream (TTFT=None).
    - Prompt caching is enabled by sending a stable prompt_cache_key and by keeping
      the static prompt prefix in a separate system message.
    """

    load_dotenv()
    apikey = os.getenv("OPENAI_API_KEY")
    if not apikey:
        raise ValueError("OPENAI_API_KEY not set in environment.")

    client = OpenAI(api_key=apikey)
    messages, parts = _build_messages(
        task_description,
        drone,
        execution_history=execution_history,
        current_status=current_status,
        mission_context=mission_context,
        planning_mode=planning_mode,
        conversation_history=conversation_history,
    )

    try:
        static_prompt_tokens = _count_messages_tokens_via_api(
            client,
            model=PLANNER_MODEL,
            messages=[{"role": "system", "content": parts.static_prefix}] if parts.static_prefix else [],
        ) if parts.static_prefix else 0
    except Exception:
        try:
            static_prompt_tokens = _count_messages_tokens_locally(
                model=PLANNER_MODEL,
                messages=[{"role": "system", "content": parts.static_prefix}] if parts.static_prefix else [],
            ) if parts.static_prefix else 0
        except Exception:
            static_prompt_tokens = None

    log_trace(f"[LLM API] Static Prompt Prefix:\n{parts.static_prefix}")
    log_trace(f"[LLM API] Dynamic Prompt Suffix:\n{parts.dynamic_suffix}")
    if static_prompt_tokens is not None:
        log_verbose(f"[LLM] Static prompt tokens: {static_prompt_tokens}")
    log_verbose("[DSL] Generating flight plan...")

    stop_event = threading.Event()
    spinner_thread = None
    if show_spinner:
        spinner_thread = threading.Thread(target=_spinner_task, args=(stop_event,), daemon=True)
        spinner_thread.start()

    # Anchor TTFT / total planning time to task submission when provided.
    t_start = float(submitted_at_s) if submitted_at_s is not None else time.time()
    ttft_s: Optional[float] = None
    chunks: list[str] = []
    usage_fields: Dict[str, Optional[int]] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
    }

    def _emit(tok: str):
        nonlocal ttft_s
        if ttft_s is None:
            ttft_s = time.time() - t_start
        chunks.append(tok)
        if on_token is not None:
            try:
                on_token(tok)
            except Exception:
                pass

    request_kwargs = {
        "messages": messages,
        "temperature": 0,
        "prompt_cache_key": PROMPT_CACHE_KEY,
        "prompt_cache_retention": PROMPT_CACHE_RETENTION,
    }

    try:
        if stream:
            try:
                resp = _chat_completion_create(
                    client,
                    model=PLANNER_STREAM_MODEL,
                    stream=True,
                    stream_options={"include_usage": True},
                    **request_kwargs,
                )
                for event in resp:
                    try:
                        if getattr(event, "usage", None) is not None:
                            usage_fields = _extract_usage_fields(event.usage)
                        if not getattr(event, "choices", None):
                            continue
                        delta = event.choices[0].delta
                        tok = getattr(delta, "content", None)
                        if tok:
                            _emit(tok)
                    except Exception:
                        continue
            except Exception as e:
                log_verbose(f"[LLM] Streaming failed, falling back to non-stream: {e}")
                stream = False

        if not stream:
            response = _chat_completion_create(
                client,
                model=PLANNER_MODEL,
                stream=False,
                **request_kwargs,
            )
            content = response.choices[0].message.content or ""
            chunks = [content]
            ttft_s = None
            usage_fields = _extract_usage_fields(getattr(response, "usage", None))

    finally:
        if show_spinner:
            stop_event.set()
            if spinner_thread is not None:
                spinner_thread.join()

    dsl = "".join(chunks).strip()
    t_total = time.time() - t_start

    log_trace(f"[LLM] Generated Plan: {dsl}")
    if usage_fields["cached_tokens"] is not None:
        log_verbose(
            "[LLM] Usage: "
            f"prompt_tokens={usage_fields['prompt_tokens']}, "
            f"cached_tokens={usage_fields['cached_tokens']}, "
            f"completion_tokens={usage_fields['completion_tokens']}, "
            f"total_tokens={usage_fields['total_tokens']}"
        )
    log_verbose("[DSL] Flight plan ready")

    if print_plan:
        print(f"\n\033[1;32mFlight Plan: {dsl}\033[0m\n")

    timings = PlanTimings(
        ttft_ms=(1000.0 * ttft_s) if ttft_s is not None else None,
        total_ms=1000.0 * t_total,
        prompt_tokens=usage_fields["prompt_tokens"],
        completion_tokens=usage_fields["completion_tokens"],
        total_tokens=usage_fields["total_tokens"],
        cached_tokens=usage_fields["cached_tokens"],
        static_prompt_tokens=static_prompt_tokens,
    )
    return dsl, timings


def get_dsl(task_description, drone):
    """Interactive helper that generates a DSL plan and prints it."""
    dsl, _timings = plan_dsl(
        task_description,
        drone,
        stream=False,  # keep stable behaviour unless you want streaming in interactive
        show_spinner=True,
        print_plan=True,
    )
    return dsl


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None

    candidates: List[str] = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    return None


def review_dsl(
    task_description: str,
    dsl: str,
    drone,
    *,
    execution_history=None,
    current_status=None,
    mission_context: Optional[str] = None,
    planning_mode: Optional[str] = None,
    conversation_history=None,
) -> PlanReview:
    """Run a low-temperature self-critique over a proposed DSL plan."""
    dsl = (dsl or "").strip()
    if not dsl:
        return PlanReview(summary="No DSL plan to review.")

    apikey = os.getenv("OPENAI_API_KEY")
    if not apikey:
        return PlanReview(summary="Plan review unavailable.")

    try:
        client = OpenAI(api_key=apikey)
        assets = _load_prompt_assets(drone)
        critic_prompt = f"""You are reviewing a DSL-based UAV flight plan before execution.
Return ONLY valid JSON. Do not include markdown fences or commentary.

Required JSON keys:
- requires_clarification: boolean
- clarification_question: string
- ambiguity_reasons: array of strings
- safety_risks: array of strings
- plan_issues: array of strings
- suggested_revision_needed: boolean
- revised_dsl: string
- summary: string
- confidence: number between 0 and 1

Decision rules:
- Set requires_clarification=true when the user request is still materially ambiguous or unsafe to execute without asking.
- Ask at most one concise clarification question.
- Set suggested_revision_needed=true only when the existing plan can be safely improved without changing the mission intent.
- If clarification is required, revised_dsl should usually be an empty string.
- Preserve the available skills and valid DSL syntax.
- Be conservative about safety and ambiguity.

Low-level skills:
{assets['low_level_skills']}

High-level skills:
{assets['high_level_skills']}

DSL syntax reference:
{assets['dsl_syntax']}
"""
        critic_user = f"""User task:
{task_description}

Current DSL plan:
{dsl}

Execution history:
{_format_execution_history(execution_history)}

Current UAV status:
{_stringify_status(current_status if current_status is not None else getattr(drone, 'get_status_dict', drone.get_status)())}

Mission context:
{mission_context or ''}

Planning mode:
{planning_mode or 'initial'}

Conversation history:
{_format_conversation_history(conversation_history)}
"""
        response = _chat_completion_create(
            client,
            model=PLANNER_MODEL,
            stream=False,
            temperature=0,
            prompt_cache_key="talk-to-fly:critic:v1",
            prompt_cache_retention=PROMPT_CACHE_RETENTION,
            messages=[
                {"role": "system", "content": critic_prompt},
                {"role": "user", "content": critic_user},
            ],
        )
        content = response.choices[0].message.content or ""
        payload = _extract_json_object(content) or {}
        review = PlanReview(
            requires_clarification=bool(payload.get("requires_clarification", False)),
            clarification_question=(str(payload.get("clarification_question", "")).strip() or None),
            ambiguity_reasons=tuple(str(x) for x in (payload.get("ambiguity_reasons") or [])),
            safety_risks=tuple(str(x) for x in (payload.get("safety_risks") or [])),
            plan_issues=tuple(str(x) for x in (payload.get("plan_issues") or [])),
            suggested_revision_needed=bool(payload.get("suggested_revision_needed", False)),
            revised_dsl=(str(payload.get("revised_dsl", "")).strip() or None),
            summary=str(payload.get("summary", "")).strip(),
            confidence=(float(payload.get("confidence")) if payload.get("confidence") is not None else None),
        )
        if review.requires_clarification and not review.clarification_question:
            review = PlanReview(
                requires_clarification=True,
                clarification_question="Please clarify the remaining ambiguous part of the mission before takeoff.",
                ambiguity_reasons=review.ambiguity_reasons,
                safety_risks=review.safety_risks,
                plan_issues=review.plan_issues,
                suggested_revision_needed=False,
                revised_dsl=None,
                summary=review.summary,
                confidence=review.confidence,
            )
        return review
    except Exception as exc:
        log_verbose(f"[LLM] Plan review failed; proceeding without critique: {exc}")
        return PlanReview(summary=f"Plan review unavailable: {exc}")
