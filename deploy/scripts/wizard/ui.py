"""Visual primitives used everywhere in the wizard: shared Rich console,
banner, section header, single-line spinner, ok/warn/fail and a couple of
small prompt helpers.

Kept i18n-free so it can be imported by `i18n.py` itself without a circular
dep — translation strings are looked up in the callers via `t(...)`.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.text import Text


console = Console()


# ASCII logo — printed at the top of every banner. Compact 2 lines so it
# doesn't dominate small terminal heights.
LOGO = (
    "█▀▄ █░█░█ █▀▀ █░░ █░░ █▀▀ █▀█ █▀▄\n"
    "█▄▀ ▀▄▀▄▀ █▄▄ █▄▄ █▄▄ █▄▄ █▀▄ █▄▀"
)


def banner(subtitle: str = "") -> None:
    """Centered logo + optional subtitle line. Use after lang pick."""
    console.print(Align.center(Text(LOGO, style="bold magenta")))
    if subtitle:
        console.print(Align.center(Text(subtitle, style="dim cyan")))
    console.print()


def banner_minimal() -> None:
    """Just the logo — used before language is chosen, no subtitle yet."""
    console.print(Align.center(Text(LOGO, style="bold magenta")))
    console.print()


def section(title: str) -> None:
    """Bold-magenta section header with a leading blank line."""
    console.print(f"\n[bold magenta]── {title} ──[/bold magenta]")


def step(label: str, work: Callable, delay: float = 0.3):
    """Run `work()` while showing a Rich spinner with `label`. On success
    the spinner is replaced with a green ✓ line and the result returned.
    On exception the line becomes a red ✗ and the exception re-raises."""
    spinner = Spinner("dots", text=Text(label, style="cyan"), style="cyan")
    with Live(spinner, console=console, refresh_per_second=12, transient=True):
        try:
            result = work()
        except Exception:
            console.print(f"  [red]✗[/red] {label}")
            raise
        time.sleep(delay)
    console.print(f"  [green]✓[/green] {label}")
    return result


def ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def warn_line(msg: str) -> None:
    console.print(f"  [yellow]![/yellow] {msg}")


def fail(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


def ask_seconds(prompt_key: str, *, default: int, minimum: int) -> int:
    """Ask for an interval in seconds. Looks up `prompt_key` and the standard
    `min_clamp` warning via i18n; falls back to `default` on bad input;
    clamps anything below `minimum` to `minimum`."""
    # Local import to dodge the i18n→ui circular at module-load time.
    from .i18n import t
    raw = Prompt.ask(f"  {t(prompt_key)}", default=str(default))
    try:
        n = int(raw)
    except ValueError:
        n = default
    if n < minimum:
        warn_line(t("min_clamp", minimum=minimum))
        n = minimum
    return n


def confirm_install(*, title: str, size: str, detail: str, prompt: str) -> bool:
    """Pretty pre-install prompt — used before npm install / build. Default
    Yes (Enter alone proceeds). DWELLERD_YES=1 short-circuits for CI."""
    if os.environ.get("DWELLERD_YES") == "1":
        console.print(
            f"  [cyan]{title}[/cyan]   [dim]{size} · auto-yes (DWELLERD_YES=1)[/dim]"
        )
        return True
    console.print()
    console.print(f"  [cyan]{title}[/cyan]   [dim]{size}[/dim]")
    console.print(f"  [dim]{detail}[/dim]")
    return Confirm.ask(f"  {prompt}", default=True)
