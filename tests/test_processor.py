"""The log pipeline: filtering, dedup, the burst cap and the digest."""
import asyncio

import pytest

from dwellerd.logs.processor import LogProcessor
from dwellerd.logs.sources.base import LogSource
from dwellerd.storage import Storage


class FakeSource(LogSource):
    """Yields a fixed list of lines, then blocks so the consumer stays
    alive instead of hot-looping on a restart."""

    def __init__(self, name, lines, pattern=".+"):
        super().__init__(name=name, pattern=pattern, reconnect_delay=3600)
        self.lines = lines

    async def stream(self):
        for line in self.lines:
            yield line
        await asyncio.Event().wait()


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "logs.sqlite")
    s.connect()
    yield s
    s.close()


async def drain(processor, ticks=6):
    """Let the consumer tasks run, then tear them down."""
    task = asyncio.create_task(processor.run())
    for _ in range(ticks):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def build(store, lines, **kwargs):
    fired = []
    digests = []

    async def on_first(source, sample):
        fired.append((source, sample))

    async def on_digest(items, period):
        digests.append(items)

    processor = LogProcessor(
        storage=store, sources=[FakeSource("app", lines)],
        on_first=on_first, on_digest=on_digest, **kwargs,
    )
    return processor, fired, digests


async def test_one_alert_per_kind_of_error(store):
    lines = [
        "ERROR connection refused id=1",
        "ERROR connection refused id=2",
        "ERROR connection refused id=3",
    ]
    processor, fired, _ = build(store, lines, level="error")
    await drain(processor)
    assert len(fired) == 1
    # All three were still captured — dedup silences alerts, not storage.
    assert store.log_count_since(0) == 3


async def test_level_filter_drops_non_errors(store):
    lines = ["INFO all good", "DEBUG chatter", "ERROR it broke"]
    processor, fired, _ = build(store, lines, level="error")
    await drain(processor)
    assert len(fired) == 1
    assert store.log_count_since(0) == 1


async def test_level_all_keeps_everything(store):
    processor, _, _ = build(store, ["INFO a", "DEBUG b"], level="all")
    await drain(processor)
    assert store.log_count_since(0) == 2


async def test_source_regex_is_applied_on_top_of_the_level(store):
    fired = []

    async def on_first(source, sample):
        fired.append(sample)

    processor = LogProcessor(
        storage=store, level="all", on_first=on_first,
        sources=[FakeSource("app", ["ERROR keep me", "ERROR drop me"],
                            pattern="keep")],
    )
    await drain(processor)
    assert len(fired) == 1 and "keep me" in fired[0]


async def test_instant_alerts_are_burst_capped(store):
    # Ten genuinely distinct new errors at once must not become ten
    # messages. The words have to differ: a trailing counter would be
    # normalised away and collapse into a single signature.
    words = ["alpha", "bravo", "charlie", "delta", "echo",
             "foxtrot", "golf", "hotel", "india", "juliet"]
    lines = [f"ERROR failure in the {w} subsystem" for w in words]
    processor, fired, _ = build(store, lines, level="error")
    await drain(processor, ticks=30)
    assert len(fired) == 5                    # _FIRST_BURST
    assert store.log_count_since(0) == 10     # nothing was dropped


async def test_notify_false_captures_without_alerting(store):
    processor, fired, _ = build(store, ["ERROR boom"], level="error", notify=False)
    await drain(processor)
    assert fired == []
    assert store.log_count_since(0) == 1


async def test_digest_groups_and_ranks(store):
    lines = ["ERROR noisy id=1", "ERROR noisy id=2", "ERROR rare thing"]
    processor, _, digests = build(store, lines, level="error")
    await drain(processor)
    await processor.flush_digest()
    assert len(digests) == 1
    assert [item["count"] for item in digests[0]] == [2, 1]


async def test_digest_resets_after_a_flush(store):
    processor, _, digests = build(store, ["ERROR boom"], level="error")
    await drain(processor)
    await processor.flush_digest()
    await processor.flush_digest()
    assert len(digests) == 1      # the second flush had nothing to say
