"""Request-scoped audit context.

We use a ``ContextVar`` rather than a threadlocal because Django can run
views under ASGI concurrency where the same thread services multiple
requests. ContextVar gives us per-task isolation for free.

The middleware binds the current actor/IP on request entry; services deep
in the stack read this context to attribute audit log entries without
plumbing ``request`` through every function signature.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    actor: object | None
    ip: str


_ctx: ContextVar[RequestContext | None] = ContextVar("lumina_audit_ctx", default=None)


def bind_request(*, actor: object | None, ip: str) -> None:
    """Set the context for the current request/task."""
    _ctx.set(RequestContext(actor=actor, ip=ip))


def clear_request() -> None:
    _ctx.set(None)


def current() -> RequestContext | None:
    return _ctx.get()
