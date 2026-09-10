"""Terminal primitives for the wizard: shared console, banner, sections,
spinner-backed steps and the ok/warn/fail lines.

Kept free of translation lookups so `i18n.py` can import it without a
circular dependency — callers pass already-translated strings in.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable

from rich.align import Align
from rich.console import Console, Group
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


def _number_selection(raw: str, options: list[str]) -> list[str]:
    """Parse the non-interactive fallback: ``1 3``, ``all`` or blank."""
    raw = raw.strip().lower()
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


def _picker_view(
    prompt: str, labels: list[str], selected: set[int], cursor: int, hint: str,
) -> Group:
    """Render a cursor-centred window so long service lists fit a terminal."""
    visible = max(5, min(len(labels), console.height - 7))
    start = max(0, min(cursor - visible // 2, len(labels) - visible))
    stop = min(len(labels), start + visible)

    rows: list[Text] = [Text(f"  {prompt}", style="bold")]
    if hint:
        rows.append(Text(f"  {hint}", style="dim"))
    if start:
        rows.append(Text(f"    … {start}", style="dim"))
    for index in range(start, stop):
        active = index == cursor
        checked = index in selected
        row = Text("  › " if active else "    ", style="bold cyan" if active else "")
        row.append("[●] " if checked else "[ ] ", style="green" if checked else "dim")
        row.append(labels[index])
        rows.append(row)
    if stop < len(labels):
        rows.append(Text(f"    … {len(labels) - stop}", style="dim"))
    return Group(*rows)


def _interactive_selection(
    prompt: str, options: list[str], labels: list[str], default_all: bool,
    hint: str,
) -> list[str]:
    """TTY checkbox picker: arrows/J/K, Space, A, Enter."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    cursor = 0
    selected = set(range(len(options))) if default_all else set()
    try:
        tty.setcbreak(fd)
        with Live(
            _picker_view(prompt, labels, selected, cursor, hint),
            console=console, auto_refresh=False, transient=False,
        ) as live:
            while True:
                key = sys.stdin.read(1)
                if key == "\x1b":
                    # Arrow keys arrive as ESC + two characters. Read through
                    # TextIO's own buffer; polling the underlying fd can miss
                    # bytes Python has already buffered.
                    sequence = sys.stdin.read(2)
                    if sequence == "[A":
                        cursor = (cursor - 1) % len(options)
                    elif sequence == "[B":
                        cursor = (cursor + 1) % len(options)
                elif key in ("k", "K"):
                    cursor = (cursor - 1) % len(options)
                elif key in ("j", "J"):
                    cursor = (cursor + 1) % len(options)
                elif key == " ":
                    if cursor in selected:
                        selected.remove(cursor)
                    else:
                        selected.add(cursor)
                elif key in ("a", "A"):
                    selected = set() if len(selected) == len(options) \
                        else set(range(len(options)))
                elif key in ("\r", "\n"):
                    break
                live.update(
                    _picker_view(prompt, labels, selected, cursor, hint), refresh=True,
                )
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
    return [option for index, option in enumerate(options) if index in selected]


def choose_many(
    prompt: str, options: list[str], default_all: bool = False,
    display_options: list[str] | None = None, hint: str = "",
) -> list[str]:
    """Select multiple values with checkboxes, with a numeric fallback."""
    if not options:
        return []
    labels = list(display_options or options)
    if len(labels) != len(options):
        raise ValueError("display_options must match options")

    if os.name == "posix" and sys.stdin.isatty() and console.is_terminal:
        try:
            return _interactive_selection(prompt, options, labels, default_all, hint)
        except (OSError, ValueError):
            # Dumb or unusual TTY: the old numbered input remains usable.
            pass

    for index, label in enumerate(labels, 1):
        console.print(f"    [cyan]{index}[/cyan]. {label}")
    hint = "all" if default_all else ""
    raw = Prompt.ask(f"  {prompt} [1 3 / all]", default=hint)
    return _number_selection(raw, options)
