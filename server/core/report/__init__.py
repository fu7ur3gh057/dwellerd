import logging

from ..notifiers.base import Notifier
from .sections.base import Section
from .sections.dlq import DlqSection
from .sections.docker import DockerComposeSection
from .sections.postgres import PostgresSection
from .sections.recent_errors import RecentErrorsSection
from .sections.vps import VpsSection

log = logging.getLogger(__name__)


def build_report_context(
    raw: dict,
    notifiers_by_type: dict[str, Notifier],
    logs_enabled: bool = False,
) -> dict | None:
    """Returns {hostname, lang, sections, targets} or None if nothing to render
    or no notifier picked up. Caller schedules the actual digest via TaskIQ."""
    hostname = raw.get("hostname", "")

    selected = raw.get("notifier")
    if selected:
        target = [notifiers_by_type[selected]] if selected in notifiers_by_type else []
        if not target:
            log.warning("report: notifier %r not found in config", selected)
    else:
        target = list(notifiers_by_type.values())

    lang = raw.get("lang") or (getattr(target[0], "lang", "en") if target else "en")
    sections = _build_sections(raw, lang=lang, logs_enabled=logs_enabled)
    if not sections or not target:
        return None

    return {
        "hostname": hostname,
        "lang": lang,
        "sections": sections,
        "targets": target,
    }


def _build_sections(
    raw: dict,
    lang: str = "en",
    logs_enabled: bool = False,
) -> list[Section]:
    sections: list[Section] = []
    host = raw.get("host") or {}

    if host:
        # New schema: host.disks (list of paths), host.interfaces (list)
        # Legacy schema: host.disks.paths, host.net.interfaces — handled too.
        disks = host.get("disks")
        if isinstance(disks, dict):
            disks = disks.get("paths")
        if not disks:
            disks = ["/"]

        interfaces = host.get("interfaces")
        if interfaces is None:
            net = host.get("net")
            if isinstance(net, dict):
                interfaces = net.get("interfaces")
            elif isinstance(net, list):
                interfaces = net

        sections.append(VpsSection(
            lang=lang,
            disks=disks,
            interfaces=interfaces,
            warn_pct=float(host.get("warn_pct", 80)),
        ))

    docker = raw.get("docker") or []
    if docker:
        sections.append(DockerComposeSection(projects=docker, lang=lang))

    for pg in raw.get("postgres") or []:
        sections.append(PostgresSection(**pg))
    for dlq in raw.get("dlq") or []:
        sections.append(DlqSection(**dlq))

    if logs_enabled:
        re_cfg = raw.get("recent_errors")
        if re_cfg is not False:
            re_cfg = re_cfg if isinstance(re_cfg, dict) else {}
            sections.append(RecentErrorsSection(
                limit=int(re_cfg.get("limit", 5)),
                lang=lang,
            ))

    return sections
