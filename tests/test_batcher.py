"""Grouping: simultaneous alerts become one message, and none is ever
lost between being queued and being sent."""
import asyncio

import pytest

from dwellerd.notify.batcher import AlertBatcher


class Sink:
    def __init__(self):
        self.batches = []

    async def __call__(self, items):
        self.batches.append(list(items))


async def test_alerts_inside_the_window_arrive_together():
    sink = Sink()
    b = AlertBatcher(sink, window=0.05)
    await b.add("a")
    await b.add("b", silent=True)
    await asyncio.sleep(0.15)
    assert sink.batches == [[("a", False), ("b", True)]]


async def test_a_lone_alert_is_delivered_after_the_window():
    sink = Sink()
    b = AlertBatcher(sink, window=0.05)
    await b.add("a")
    assert sink.batches == []
    await asyncio.sleep(0.15)
    assert sink.batches == [[("a", False)]]


async def test_alerts_in_separate_windows_are_separate_batches():
    sink = Sink()
    b = AlertBatcher(sink, window=0.05)
    await b.add("a")
    await asyncio.sleep(0.15)
    await b.add("b")
    await asyncio.sleep(0.15)
    assert [len(x) for x in sink.batches] == [1, 1]


async def test_window_zero_delivers_immediately():
    sink = Sink()
    b = AlertBatcher(sink, window=0)
    await b.add("a")
    await b.add("b")
    assert sink.batches == [[("a", False)], [("b", False)]]


async def test_close_delivers_what_is_pending_without_waiting():
    sink = Sink()
    b = AlertBatcher(sink, window=60)
    await b.add("a")
    await b.add("b")
    await b.close()
    assert sink.batches == [[("a", False), ("b", False)]]


async def test_close_with_nothing_pending_is_a_no_op_and_reusable():
    sink = Sink()
    b = AlertBatcher(sink, window=0.05)
    await b.close()
    await b.add("a")
    await asyncio.sleep(0.15)
    assert sink.batches == [[("a", False)]]


async def test_close_twice_does_not_duplicate():
    sink = Sink()
    b = AlertBatcher(sink, window=60)
    await b.add("a")
    await b.close()
    await b.close()
    assert len(sink.batches) == 1


async def test_close_waits_for_a_delivery_already_in_flight():
    started, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def slow(items):
        started.set()
        await release.wait()
        delivered.append(items)

    b = AlertBatcher(slow, window=0.01)
    await b.add("a")
    await started.wait()                 # the timer fired, send is mid-flight
    closer = asyncio.create_task(b.close())
    await asyncio.sleep(0.05)
    assert not closer.done()             # close must not return before the send
    release.set()
    await closer
    assert delivered == [[("a", False)]]


async def test_a_failing_delivery_does_not_raise_or_wedge_the_batcher():
    calls = []

    async def boom(items):
        calls.append(items)
        raise RuntimeError("render bug")

    b = AlertBatcher(boom, window=0)
    await b.add("a")                      # logged, not raised
    await b.add("b")
    assert len(calls) == 2


async def test_alerts_added_during_a_delivery_get_their_own_batch():
    gate = asyncio.Event()
    batches = []

    async def deliver(items):
        batches.append(items)
        if len(batches) == 1:
            await gate.wait()

    b = AlertBatcher(deliver, window=0.01)
    await b.add("a")
    await asyncio.sleep(0.05)             # first batch is now blocked in deliver
    await b.add("b")                      # must schedule a fresh timer
    gate.set()
    await asyncio.sleep(0.1)
    await b.close()
    assert [x[0][0] for x in batches] == ["a", "b"]
