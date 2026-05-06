"""Process-wide runtime context, attached to broker.state.

Tasks inject this via `TaskiqDepends(get_app_context)`. State that needs
to survive restarts now lives in SQLite (`db.models`); this
dataclass holds the still-volatile pieces — built handlers, notifier
instances, report sections — that get rebuilt on every boot from config.
"""

from dataclasses import dataclass, field

from core.checks import Check
from config import Config
from core.notifiers.base import Notifier
from core.report.sections.base import Section


@dataclass
class AppContext:
    config: Config
    checks_by_name: dict[str, Check] = field(default_factory=dict)
    notifiers_by_type: dict[str, Notifier] = field(default_factory=dict)
    notifiers: list[Notifier] = field(default_factory=list)
    # Report
    report_hostname: str = ""
    report_lang: str = "en"
    report_sections: list[Section] = field(default_factory=list)
    report_targets: list[Notifier] = field(default_factory=list)
    # Logs — set when a `logs:` block is present in config; the actual
    # `LogEventStore` instance lives on broker.state.log_store so the
    # processor, REST routes and report sections share one handle.
    logs_enabled: bool = False
