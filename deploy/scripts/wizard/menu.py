"""Top-level interactive menu. Returns one of: 'dev', 'service', 'uninstall',
'edit', 'exit'.
"""
from __future__ import annotations

from rich.prompt import Prompt

from .i18n import t
from .paths import DEV_CONFIG, PROD_CONFIG
from .ui import console


def main_menu() -> str:
    if PROD_CONFIG.exists():
        console.print(
            f"[green]✓[/green] {t('config_found')}: [cyan]{PROD_CONFIG}[/cyan]"
        )
    elif DEV_CONFIG.exists():
        console.print(
            f"[green]✓[/green] {t('config_found')}: [cyan]{DEV_CONFIG}[/cyan]"
        )
    else:
        console.print(f"[yellow]·[/yellow] [italic dim]{t('config_missing')}[/italic dim]")

    console.print()
    console.print(f"  [bold]{t('menu_title')}[/bold]")
    console.print(f"    [bold cyan]1[/bold cyan])  {t('menu_dev')}")
    console.print(f"    [bold cyan]2[/bold cyan])  {t('menu_service')}")
    console.print(f"    [bold cyan]3[/bold cyan])  {t('menu_uninstall')}")
    console.print(f"    [bold cyan]4[/bold cyan])  {t('menu_edit')}")
    console.print(f"    [bold cyan]q[/bold cyan])  [dim]{t('menu_exit')}[/dim]")

    choice = Prompt.ask(
        f"[bold]{t('choice')}[/bold]",
        choices=["1", "2", "3", "4", "q"],
        default="1",
        show_choices=False,
    )
    return {
        "1": "dev", "2": "service", "3": "uninstall", "4": "edit", "q": "exit",
    }[choice]
