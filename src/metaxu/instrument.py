"""Instrumentation helpers: make existing agent code observable.

The guiding principle is that adopting Metaxu should feel like adding
logging — decorate the tools an agent already has, and the active
:class:`~metaxu.session.AssuranceSession` picks the calls up automatically
via a context variable (safe under threads and asyncio tasks).

Usage::

    @assured_tool(tags=["platelet_count"], version="1.2.0")
    def get_platelet_count(patient_id: str) -> dict: ...

    with AssuranceSession(question=q) as session:
        get_platelet_count("pat-001")   # recorded, tagged, timed
"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable, TypeVar

from .session import current_session

F = TypeVar("F", bound=Callable[..., Any])


def assured_tool(
    name: str | None = None,
    tags: list[str] | None = None,
    version: str | None = None,
) -> Callable[[F], F]:
    """Decorator that records every call to the wrapped tool.

    Records the tool name, arguments, a result summary, errors, and wall
    time into the active session. Outside a session the tool behaves
    exactly as before — instrumentation is never load-bearing.
    """

    def decorator(func: F) -> F:
        tool_name = name or func.__name__

        def _bind_arguments(args: tuple, kwargs: dict) -> dict[str, Any]:
            try:
                bound = inspect.signature(func).bind(*args, **kwargs)
                bound.apply_defaults()
                return dict(bound.arguments)
            except TypeError:
                return {"args": list(args), "kwargs": kwargs}

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session()
            if session is None:
                return func(*args, **kwargs)
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                session.record_tool_call(
                    name=tool_name,
                    arguments=_bind_arguments(args, kwargs),
                    error=f"{type(exc).__name__}: {exc}",
                    tags=tags,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    version=version,
                )
                raise
            session.record_tool_call(
                name=tool_name,
                arguments=_bind_arguments(args, kwargs),
                result=result,
                tags=tags,
                duration_ms=(time.perf_counter() - start) * 1000,
                version=version,
            )
            return result

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session()
            if session is None:
                return await func(*args, **kwargs)
            start = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                session.record_tool_call(
                    name=tool_name,
                    arguments=_bind_arguments(args, kwargs),
                    error=f"{type(exc).__name__}: {exc}",
                    tags=tags,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    version=version,
                )
                raise
            session.record_tool_call(
                name=tool_name,
                arguments=_bind_arguments(args, kwargs),
                result=result,
                tags=tags,
                duration_ms=(time.perf_counter() - start) * 1000,
                version=version,
            )
            return result

        chosen = async_wrapper if inspect.iscoroutinefunction(func) else wrapper
        return chosen  # type: ignore[return-value]

    return decorator
