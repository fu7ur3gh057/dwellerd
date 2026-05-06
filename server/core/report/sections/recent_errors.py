import time

from .base import SectionResult

_LABELS = {
    "en": {"title": "🔄 Recent errors", "empty": "no recent errors"},
    "ru": {"title": "🔄 Последние ошибки", "empty": "ошибок не было"},
}


class RecentErrorsSection:
    """Pulls the last N unique-by-signature events from `LogEventStore`.

    The store is shared across the daemon — REST routes, the processor's
    fan-out and this section read from the same `broker.state.log_store`.
    """

    def __init__(self, limit: int = 5, lang: str = "en") -> None:
        self.limit = max(1, int(limit))
        self.lang = lang

    async def render(self) -> SectionResult:
        L = _LABELS.get(self.lang, _LABELS["en"])
        events = await self._tail()
        if not events:
            return SectionResult(text=f"{L['title']}\n— {L['empty']}")

        lines = [L["title"]]
        for ev in events:
            ts = time.strftime("%H:%M", time.localtime(ev.get("ts", 0)))
            src = ev.get("source", "?")
            sample = ev.get("line", "").splitlines()[0] if ev.get("line") else ""
            sample = sample[:120]
            lines.append(f"• {ts} {src}: {sample}")
        return SectionResult(text="\n".join(lines))

    async def _tail(self) -> list[dict]:
        from services.taskiq.broker import broker

        store = broker.state.data.get("log_store")
        if store is None:
            return []
        # Pull a generous window, then dedup by signature (newest first).
        events = await store.tail(limit=self.limit * 20)
        seen: set[str] = set()
        unique: list[dict] = []
        for ev in events:
            sig = ev.get("sig")
            if sig and sig not in seen:
                seen.add(sig)
                unique.append(ev)
                if len(unique) >= self.limit:
                    break
        # Caller renders chronological order.
        return list(reversed(unique))
