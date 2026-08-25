"""Terminal primitives for the wizard: shared console, banner, sections,
spinner-backed steps and the ok/warn/fail lines.

Kept free of translation lookups so `i18n.py` can import it without a
circular dependency — callers pass already-translated strings in.
"""
from __future__ import annotations

import time
from typing import Callable

from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.text import Text

console = Console()

LOGO = (
    "█▀▄ █░█░█ █▀▀ █░░ █░░ █▀▀ █▀█ █▀▄\n"
    "█▄▀ ▀▄▀▄▀ █▄▄ █▄▄ █▄▄ █▄▄ █▀▄ █▄▀"
)


def banner(subtitle: str = "") -> None:
    console.print()
    console.print(Align.center(Text(LOGO, style="bold cyan")))
    if subtitle:
        console.print(Align.center(Text(subtitle, style="dim")))
    console.print()


def section(title: str) -> None:
    console.print(f"\n[bold cyan]── {title} ──[/bold cyan]")


def ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]![/yellow] {msg}")


def fail(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


def info(msg: str) -> None:
    console.print(f"  [dim]{msg}[/dim]")


def step(label: str, work: Callable, delay: float = 0.2):
    """Run `work()` behind a spinner; leave a ✓ or ✗ line behind."""
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


def ask(prompt: str, default: str = "") -> str:
    return Prompt.ask(f"  {prompt}", default=default).strip()


def ask_int(prompt: str, default: int, minimum: int = 0) -> int:
    raw = Prompt.ask(f"  {prompt}", default=str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def ask_float(prompt: str, default: float, minimum: float = 0.0) -> float:
    raw = Prompt.ask(f"  {prompt}", default=str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, value)


def confirm(prompt: str, default: bool = True) -> bool:
    return Confirm.ask(f"  {prompt}", default=default)


def choose_many(prompt: str, options: list[str], default_all: bool = False) -> list[str]:
    """Numbered multi-select. Accepts `1 3 5`, `1,3,5`, `all`, or empty for
    none. Returns the chosen option strings."""
    if not options:
        return []
    for index, option in enumerate(options, 1):
        console.print(f"    [cyan]{index}[/cyan]. {option}")
    hint = "all" if default_all else ""
    raw = Prompt.ask(f"  {prompt}", default=hint).strip().lower()
    if not raw:
        return []
    if raw in ("all", "*", "все"):
        return list(options)
    picked: list[str] = []
    for token in raw.replace(",", " ").split():
        try:
            index = int(token)
        except ValueError:
            continue
        if 1 <= index <= len(options) and options[index - 1] not in picked:
            picked.append(options[index - 1])
    return picked
