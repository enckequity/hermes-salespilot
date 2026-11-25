"""Native approval for SalesPilot's sending and approval-ledger tools.

A ``pre_tool_call`` hook asks the engine's own approval gate (the same card a dangerous command
gets on every chat surface) before ``salespilot_send_email`` or an approve/reject through
``salespilot_request_approval`` runs. The card shows the exact sender, recipient, subject and body,
and its ``rule_key`` is unique per call, so an "always" answer can never cover another email.

The hook also leaves a one-use ticket holding a fingerprint of exactly what the card showed. The
handler re-reads the draft and only proceeds when a ticket for the same content and session exists,
so a call that skipped the hook, a draft edited after the card was shown, or arguments rewritten
by another hook are refused. Unattended runs (cron, including subagents it delegated to, and
single-query and other turns where the engine says nobody can answer) are blocked before any card is shown. Every failure blocks.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

SEND_TOOL = "salespilot_send_email"
LEDGER_TOOL = "salespilot_request_approval"
GATED = frozenset({SEND_TOOL, LEDGER_TOOL})
TICKET_TTL_SECONDS = 30 * 60
BODY_LIMIT = 2500

_LOCK = threading.Lock()
_TICKETS: dict[tuple[str, str, str], dict[str, Any]] = {}
_CTX = None


def bind(ctx) -> None:
    global _CTX
    _CTX = ctx


def fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _session(kwargs: dict[str, Any]) -> str:
    return f"{kwargs.get('session_id') or ''}|{kwargs.get('task_id') or ''}"


def issue(tool: str, draft_id: str, digest: str, kwargs: dict[str, Any]) -> None:
    now = time.monotonic()
    with _LOCK:
        for key in [k for k, v in _TICKETS.items() if v["expires"] <= now]:
            _TICKETS.pop(key, None)
        _TICKETS[(tool, draft_id, _session(kwargs))] = {"digest": digest, "expires": now + TICKET_TTL_SECONDS}


def consume(tool: str, draft_id: str, digest: str, kwargs: dict[str, Any]) -> bool:
    """True once for a ticket the hook issued for this tool, draft, content and session."""
    with _LOCK:
        ticket = _TICKETS.pop((tool, draft_id, _session(kwargs)), None)
    if ticket is None or ticket["expires"] <= time.monotonic():
        return False
    return ticket["digest"] == digest


def discard(tool: str, kwargs: dict[str, Any]) -> None:
    session = _session(kwargs)
    with _LOCK:
        for key in [k for k in _TICKETS if k[0] == tool and k[2] == session]:
            _TICKETS.pop(key, None)


def clear() -> None:
    with _LOCK:
        _TICKETS.clear()


def unattended(ctx: Any = None) -> bool:
    """True when no person can answer: a cron run (also a subagent it delegated to, which the cron
    identity hides), an engine-marked cron session, a finite unattended turn, or any turn the engine's
    approval context says nobody can answer (single-query, board-card and webhook/API runs). Fails closed."""
    ctx = _CTX if ctx is None else ctx
    current = getattr(ctx, "current_cron_execution", None) if ctx is not None else None
    try:
        if current is not None and current() is not None:
            return True
    except Exception:
        return True
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return current is None  # an engine without either signal cannot prove a person is present
    try:
        if str(get_session_env("HERMES_CRON_SESSION", "") or "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    except Exception:
        return True
    try:
        from gateway.session_finite import unattended_turn
    except ImportError:
        unattended_turn = None
    try:
        if unattended_turn is not None and unattended_turn():
            return True
    except Exception:
        return True
    reader = _no_user_reader()
    if reader is None:
        return True  # the engine cannot say whether anyone is there to answer: assume nobody is
    try:
        return bool(reader())
    except Exception:
        return True


def _no_user_reader() -> Callable[[], bool] | None:
    """The engine's own "nobody can answer an approval" check (single-query/finite turns, cron,
    unattended platforms such as webhooks and the API server), or None when it is unavailable."""
    try:
        module = importlib.import_module("tools.approval_context")
    except Exception:
        return None
    reader = getattr(module, "_no_user_can_answer", None)
    return reader if callable(reader) else None


def approvals_bypassed() -> bool:
    """True when the engine would auto-approve the card (yolo or approvals off). An engine without
    these readers still runs its own gate; a reader that errors counts as bypassed."""
    try:
        from tools.approval import _yolo_active
        from tools.approval_context import _get_approval_mode
    except Exception:
        return False
    try:
        return bool(_yolo_active()) or str(_get_approval_mode() or "").strip().lower() == "off"
    except Exception:
        return True


UNATTENDED_MESSAGE = (
    "SalesPilot refused {tool}: this is an unattended run and every email and approval needs a person to "
    "say yes. Nothing was sent. Report what is ready so the owner can approve it in chat."
)
BYPASS_MESSAGE = (
    "SalesPilot refused {tool}: approvals are switched off for this session, and an email only goes out after "
    "a person approves it. Nothing was sent or recorded. Turn approvals back on, then ask again."
)


def make_hook(send_preview: Callable[[str], tuple[dict | None, str]],
              ledger_preview: Callable[[dict], tuple[dict | None, str]]) -> Callable[..., Any]:
    """``send_preview(draft_id)`` and ``ledger_preview(args)`` return ``(card, "")`` when the call needs
    a yes, ``(None, "")`` when the handler will refuse or reuse without acting, or ``(None, reason)`` to block."""

    def pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs: Any):
        if tool_name not in GATED:
            return None
        args = args if isinstance(args, dict) else {}
        try:
            if tool_name == LEDGER_TOOL:
                decision = str(args.get("decision") or "").strip().lower()
                if decision not in {"approve", "reject"}:
                    return None  # request/pending only records that a draft waits; no yes needed
            if unattended():
                return {"action": "block", "message": UNATTENDED_MESSAGE.format(tool=tool_name)}
            if approvals_bypassed():
                return {"action": "block", "message": BYPASS_MESSAGE.format(tool=tool_name)}
            if tool_name == SEND_TOOL:
                card, reason = send_preview(str(args.get("draft_id") or "").strip())
            else:
                card, reason = ledger_preview(args)
            if reason:
                return {"action": "block", "message": reason}
            if card is None:
                return None
            issue(tool_name, card["draft_id"], card["digest"], kwargs)
            return {"action": "approve", "message": card["message"],
                    "rule_key": f"salespilot:{tool_name}:{uuid.uuid4().hex}"}
        except Exception:
            logger.exception("salespilot: approval check failed; blocking the call")
            return {"action": "block", "message": f"SalesPilot could not check {tool_name} safely. Nothing was sent."}

    return pre_tool_call


def post_tool_call(tool_name: str = "", **kwargs: Any) -> None:
    """Drop any ticket a blocked or denied call left behind, so it cannot be used later."""
    if tool_name in GATED:
        discard(tool_name, kwargs)
    return None
