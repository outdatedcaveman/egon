"""Reusable lazy-load pattern — render skeleton instantly, stream data in.

Pattern:
    container = ui.element('div')
    with container: ui.html('<spinner>')

    async def _load():
        data = await asyncio.to_thread(slow_fn)
        container.clear()
        with container: ui.html(render_data(data))

    _schedule(_load)

This module wraps that into a one-liner you can use everywhere:
    lazy_panel(slow_fn, render_fn, skeleton=...)

NOTE on scheduling: we do **not** use ui.timer() here. ui.timer attaches to the
current parent_slot, and when the user navigates away the slot is disposed while
the timer is still queued — NiceGUI then raises `RuntimeError: The parent slot
of the element has been deleted` from inside its own Timer._get_context, before
our coroutine even runs. Scheduling via `loop.call_later` keeps the task at the
event-loop level, decoupled from the disposed slot.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from nicegui import ui


def _schedule(coro_fn, delay: float = 0.05) -> None:
    """Run `coro_fn()` (a zero-arg coroutine function) after `delay` seconds,
    detached from any UI slot. Exceptions are swallowed (we already render
    error states inside the coroutine when the container is still alive).
    """
    loop = asyncio.get_event_loop()

    def _spawn():
        try:
            asyncio.create_task(coro_fn())
        except Exception:
            pass

    loop.call_later(delay, _spawn)


def skeleton_panel(title: str = "Loading", lines: int = 3) -> str:
    """Generic skeleton placeholder — pulse animation, theme-aware."""
    line_html = "".join(
        f'<div style="height:14px; background: var(--surface-2); border-radius: 4px; '
        f'margin: 8px 0; width: {(96 - i*12) if i < lines-1 else 70}%; '
        f'animation: pulse 1.5s ease-in-out infinite;"></div>'
        for i in range(lines)
    )
    return f'''
    <style>
      @keyframes pulse {{
        0%, 100% {{ opacity: 0.55; }}
        50%      {{ opacity: 0.95; }}
      }}
    </style>
    <div class="panel"><div class="pbody">
      <div style="color: var(--muted); font-size: 12px; margin-bottom: 8px;">⏳ {title}…</div>
      {line_html}
    </div></div>
    '''


def _container_alive(container) -> bool:
    """True if the container's parent slot still exists (i.e. the user hasn't
    navigated away). Accessing `.parent_slot` raises RuntimeError once the
    element has been disposed.
    """
    try:
        _ = container.parent_slot
        return True
    except Exception:
        return False


def lazy_panel(load_fn: Callable, render_fn: Callable[[object], None],
               skeleton: str | None = None, delay: float = 0.05) -> None:
    """Render a skeleton, then call load_fn() in a thread, then render_fn(result).

    load_fn can be sync (slow I/O) or async. render_fn is called with the result
    inside the freshly-cleared container, on the UI thread.
    """
    container = ui.element('div')
    with container:
        ui.html(skeleton or skeleton_panel())

    async def _go():
        try:
            if asyncio.iscoroutinefunction(load_fn):
                result = await load_fn()
            else:
                result = await asyncio.to_thread(load_fn)
        except Exception as e:
            if not _container_alive(container):
                return
            try:
                container.clear()
                with container:
                    ui.html(f'<div class="flag">load error: <code>{e}</code></div>')
            except Exception:
                pass
            return
        if not _container_alive(container):
            return
        try:
            container.clear()
            with container:
                try:
                    render_fn(result)
                except Exception as e:
                    ui.html(f'<div class="flag">render error: <code>{e}</code></div>')
        except Exception:
            pass

    _schedule(_go, delay=delay)


def chip_placeholder() -> str:
    """Tiny placeholder for inline status chips — same width/height as a real chip."""
    return ('<span class="chip" style="background: var(--surface-2); color: transparent; '
            'opacity: 0.6; animation: pulse 1.5s ease-in-out infinite;">●●●●</span>')


def lazy_chip(load_fn: Callable[[], str]) -> None:
    """Render a chip placeholder, then swap to whatever load_fn returns.

    load_fn must return an HTML string (typically `<span class="chip ...">...</span>`).
    """
    el = ui.html(chip_placeholder())

    async def _go():
        try:
            html = await asyncio.to_thread(load_fn)
        except Exception as e:
            html = f'<span class="chip warn">err: {str(e)[:20]}</span>'
        if not _container_alive(el):
            return
        try:
            el.content = html
        except Exception:
            pass

    _schedule(_go)
