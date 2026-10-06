"""The buttons and commands. This is the one place the network can change
the daemon's behaviour, so the authorisation tests matter as much as the
feature tests."""
import asyncio
import time

import pytest

from dwellerd.bot import Bot
from dwellerd.storage import Storage

_real_sleep = asyncio.sleep       # tests that fake the bot's sleep still need a real one


class FakeApi:
    """Stands in for TelegramNotifier: records every Bot API call and
    answers getUpdates from a script."""

    def __init__(self, script=None):
        self.calls = []             # (method, payload)
        self.texts = []             # messages the bot sent
        self.script = list(script or [])

    def http_client(self, timeout):
        class Ctx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False
        return Ctx()

    async def api_call(self, method, payload, *, timeout=15, client=None):
        self.calls.append((method, payload))
        if method == "getUpdates":
            if not self.script:
                await asyncio.Event().wait()        # idle, like a long poll
            return self.script.pop(0)
        return 200, {"ok": True}

    async def send_text(self, text, silent=False, reply_markup=None):
        self.texts.append(text)

    def methods(self, name):
        return [p for m, p in self.calls if m == name]


CHAT = "100"


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "bot.sqlite")
    s.connect()
    yield s
    s.close()


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def bot(api, store):
    return Bot(api, store, chat_id=CHAT)


def press(data, chat=100, user=7, message_id=55, cb_id="cb1"):
    return {"update_id": 1, "callback_query": {
        "id": cb_id, "from": {"id": user}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat}},
    }}


def say(text, chat=100, user=7):
    return {"update_id": 1, "message": {"text": text, "from": {"id": user},
                                        "chat": {"id": chat}}}


def make_bad(store, name, level="crit"):
    store.save_gate(name, level, "x", confirmed=level, pending=None,
                    pending_count=0, silenced=False, last_alert=1.0)


# ── buttons ──────────────────────────────────────────────────────────────


async def test_mute_button_mutes_that_alert_and_swaps_the_buttons(bot, api, store):
    tid = store.target_id("docker:app")
    await bot.handle_update(press(f"m:{tid}:60"))
    assert store.is_muted("docker:app") and not store.is_muted("docker:db")
    (answer,) = api.methods("answerCallbackQuery")
    assert answer["callback_query_id"] == "cb1" and "docker:app" in answer["text"]
    (edit,) = api.methods("editMessageReplyMarkup")
    assert edit["chat_id"] == 100 and edit["message_id"] == 55
    (button,) = edit["reply_markup"]["inline_keyboard"][0]
    assert "Muted until" in button["text"] and button["callback_data"] == "n"


async def test_mute_duration_matches_the_button(bot, store):
    tid = store.target_id("cpu")
    before = time.time()
    await bot.handle_update(press(f"m:{tid}:480"))
    (m,) = store.active_mutes()
    assert 8 * 3600 - 5 < m["until"] - before < 8 * 3600 + 5


async def test_a_name_with_glob_characters_is_muted_literally(bot, store):
    # A button must mute exactly the alert it sits under, never a pattern.
    tid = store.target_id("disk-[a]*")
    await bot.handle_update(press(f"m:{tid}:60"))
    assert store.is_muted("disk-[a]*")
    assert not store.is_muted("disk-a") and not store.is_muted("disk-other")


async def test_ack_button_acknowledges_a_live_problem(bot, api, store):
    make_bad(store, "disk-root")
    tid = store.target_id("disk-root")
    await bot.handle_update(press(f"a:{tid}"))
    assert store.get_gate("disk-root")["acked"] is True
    assert "Acknowledged" in api.methods("editMessageReplyMarkup")[0][
        "reply_markup"]["inline_keyboard"][0][0]["text"]


async def test_ack_after_recovery_says_so_and_changes_nothing(bot, api, store):
    make_bad(store, "disk-root", "ok")
    tid = store.target_id("disk-root")
    await bot.handle_update(press(f"a:{tid}"))
    assert store.get_gate("disk-root")["acked"] is False
    assert "already back to normal" in api.methods("answerCallbackQuery")[0]["text"]
    assert api.methods("editMessageReplyMarkup") == []


async def test_a_button_whose_target_is_gone_expires(bot, api, store):
    await bot.handle_update(press("m:9999:60"))
    assert "expired" in api.methods("answerCallbackQuery")[0]["text"]
    assert store.active_mutes() == []


@pytest.mark.parametrize("data", ["", "n", "x:1", "m:1", "m:a:b", "a:", "m:1:2:3", "a:1:2"])
async def test_garbage_callback_data_does_nothing_but_answer(bot, api, store, data):
    store.target_id("cpu")
    await bot.handle_update(press(data))
    assert store.active_mutes() == []
    assert len(api.methods("answerCallbackQuery")) == 1
    assert api.methods("editMessageReplyMarkup") == []


@pytest.mark.parametrize("minutes", ["0", "-5", "10081", "99999999"])
async def test_a_forged_mute_length_is_refused(bot, store, minutes):
    tid = store.target_id("cpu")
    await bot.handle_update(press(f"m:{tid}:{minutes}"))
    assert store.active_mutes() == []


# ── authorisation ────────────────────────────────────────────────────────


async def test_buttons_from_another_chat_are_refused(bot, api, store):
    tid = store.target_id("cpu")
    await bot.handle_update(press(f"m:{tid}:60", chat=999))
    assert store.active_mutes() == []
    assert api.methods("answerCallbackQuery")[0]["text"] == "Not allowed"
    assert api.methods("editMessageReplyMarkup") == []


async def test_commands_from_another_chat_are_ignored_silently(bot, api, store):
    await bot.handle_update(say("/mute 1h", chat=999))
    assert store.active_mutes() == [] and api.texts == []


async def test_a_callback_without_a_message_is_refused(bot, api, store):
    tid = store.target_id("cpu")
    update = {"update_id": 1, "callback_query": {
        "id": "c", "from": {"id": 7}, "data": f"m:{tid}:60"}}
    await bot.handle_update(update)
    assert store.active_mutes() == []


async def test_admins_list_narrows_a_group_chat(api, store):
    bot = Bot(api, store, chat_id="-100", admins=["7"])
    tid = store.target_id("cpu")
    await bot.handle_update(press(f"m:{tid}:60", chat=-100, user=8))
    assert store.active_mutes() == []
    await bot.handle_update(say("/mute 1h", chat=-100, user=8))
    assert store.active_mutes() == [] and api.texts == []
    await bot.handle_update(press(f"m:{tid}:60", chat=-100, user=7))
    assert store.is_muted("cpu")


async def test_chat_id_comparison_is_by_string(api, store):
    # YAML may hand us an int, Telegram always does.
    bot = Bot(api, store, chat_id=-1001234567890)
    assert bot.allowed(-1001234567890, 1)
    assert not bot.allowed(-1001234567891, 1)


# ── commands ─────────────────────────────────────────────────────────────


async def test_mute_command_default_pattern_is_everything(bot, api, store):
    await bot.handle_update(say("/mute 2h"))
    (m,) = store.active_mutes()
    assert m["pattern"] == "*" and "Muted" in api.texts[0]


async def test_mute_command_with_a_pattern_and_a_bot_suffix(bot, api, store):
    await bot.handle_update(say("/mute@dweller_bot 30m docker:*"))
    assert [m["pattern"] for m in store.active_mutes()] == ["docker:*"]


async def test_mute_command_errors_do_not_mute(bot, api, store):
    await bot.handle_update(say("/mute"))
    assert "Usage" in api.texts[-1]
    await bot.handle_update(say("/mute 30"))
    assert "Cannot read" in api.texts[-1]
    await bot.handle_update(say("/mute <b>"))
    assert "&lt;b&gt;" in api.texts[-1]            # echoed input is escaped
    assert store.active_mutes() == []


async def test_unmute_variants(bot, api, store):
    store.add_mute("cpu", time.time() + 60)
    store.add_mute("memory", time.time() + 60)
    await bot.handle_update(say("/unmute cpu"))
    assert "Unmuted" in api.texts[-1] and [m["pattern"] for m in store.active_mutes()] == ["memory"]
    await bot.handle_update(say("/unmute ghost"))
    assert "Nothing to unmute" in api.texts[-1]
    await bot.handle_update(say("/unmute"))
    assert "All mutes lifted" in api.texts[-1] and store.active_mutes() == []
    await bot.handle_update(say("/unmute"))
    assert "No active mutes" in api.texts[-1]


async def test_mutes_lists_with_time_left(bot, api, store):
    await bot.handle_update(say("/mutes"))
    assert "No active mutes" in api.texts[-1]
    store.add_mute("docker:*", time.time() + 3 * 3600)
    await bot.handle_update(say("/mutes"))
    assert "docker:*" in api.texts[-1] and "left" in api.texts[-1]


async def test_ack_command(bot, api, store):
    make_bad(store, "disk-root")
    await bot.handle_update(say("/ack disk-root"))
    assert store.get_gate("disk-root")["acked"] is True and "Got it" in api.texts[-1]
    await bot.handle_update(say("/ack nonesuch"))
    assert "No such check" in api.texts[-1]
    make_bad(store, "cpu", "ok")
    await bot.handle_update(say("/ack cpu"))
    assert "already back to normal" in api.texts[-1]


async def test_ack_without_a_name_lists_what_is_wrong(bot, api, store):
    await bot.handle_update(say("/ack"))
    assert "Nothing is wrong" in api.texts[-1]
    make_bad(store, "disk-root")
    make_bad(store, "unit-<x>", "warn")
    await bot.handle_update(say("/ack"))
    assert "disk-root" in api.texts[-1] and "unit-&lt;x&gt;" in api.texts[-1]


async def test_help_and_unknown_commands(bot, api):
    await bot.handle_update(say("/help"))
    await bot.handle_update(say("/start"))
    assert len(api.texts) == 2 and "/mute" in api.texts[0]
    await bot.handle_update(say("/frobnicate"))
    await bot.handle_update(say("just chatting"))
    assert len(api.texts) == 2


async def test_russian(api, store):
    bot = Bot(api, store, chat_id=CHAT, lang="ru")
    await bot.handle_update(say("/mute 1h cpu"))
    assert "Заглушено" in api.texts[-1]


# ── polling ──────────────────────────────────────────────────────────────


async def run_for(bot, api, seconds=0.2):
    task = asyncio.create_task(bot.run())
    await _real_sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def ok(*updates):
    return 200, {"ok": True, "result": list(updates)}


async def test_backlog_is_skipped_and_polling_continues_after_it(store):
    old = press("m:1:60")
    old["update_id"] = 41
    new = say("/mute 1h cpu")
    new["update_id"] = 43
    api = FakeApi([ok(old), ok(new)])          # sync reads 41; poll then gets 43
    bot = Bot(api, store, chat_id=CHAT)
    await run_for(bot, api)
    polls = api.methods("getUpdates")
    assert polls[0] == {"offset": -1, "limit": 1, "timeout": 0}
    assert polls[1]["offset"] == 42                      # backlog confirmed away
    assert polls[2]["offset"] == 44                      # then past what it handled
    assert [m["pattern"] for m in store.active_mutes()] == ["cpu"]
    assert api.methods("setMyCommands")


async def test_nothing_pending_means_no_offset_yet(store):
    api = FakeApi([ok()])
    await run_for(Bot(api, store, chat_id=CHAT), api)
    assert "offset" not in api.methods("getUpdates")[1]


async def test_one_bad_update_does_not_stop_the_loop(store, monkeypatch):
    first, second = say("/mute 1h a"), say("/mute 1h b")
    first["update_id"], second["update_id"] = 2, 3
    api = FakeApi([ok(), ok(first, second)])
    bot = Bot(api, store, chat_id=CHAT)
    real = bot.handle_update
    calls = []

    async def flaky(update):
        calls.append(update["update_id"])
        if update["update_id"] == 2:
            raise RuntimeError("boom")
        await real(update)

    monkeypatch.setattr(bot, "handle_update", flaky)
    await run_for(bot, api)
    assert calls == [2, 3]
    assert [m["pattern"] for m in store.active_mutes()] == ["b"]


async def test_network_failure_backs_off_and_recovers(store, monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("dwellerd.bot.asyncio.sleep", fake_sleep)
    up = say("/mute 1h cpu")
    up["update_id"] = 5
    api = FakeApi([(0, {}), (0, {}), (0, {}), ok(), ok(up)])
    await run_for(Bot(api, store, chat_id=CHAT), api)
    assert sleeps[:3] == [1.0, 2.0, 4.0]                 # exponential
    assert [m["pattern"] for m in store.active_mutes()] == ["cpu"]


async def test_a_rejected_token_ends_the_loop_for_good(store):
    api = FakeApi([(401, {"ok": False})])
    bot = Bot(api, store, chat_id=CHAT)
    await asyncio.wait_for(bot.run(), 2)                 # returns, does not retry
    assert len(api.methods("getUpdates")) == 1


async def test_a_polling_conflict_waits_instead_of_spinning(store, monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("dwellerd.bot.asyncio.sleep", fake_sleep)
    api = FakeApi([(409, {"ok": False}), ok()])
    await run_for(Bot(api, store, chat_id=CHAT), api)
    assert sleeps[0] == 60.0
