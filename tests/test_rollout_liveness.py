"""A desktop turn must still count as running for the device collector.

Reported on hardware: the home-screen pet stayed asleep while Codex was plainly
working. A second app-server cannot see a turn the desktop is running on its own
thread: the listing freezes ``recencyAt`` at the moment the turn started and
keeps ``status.type`` at ``notLoaded``, so the collector buried the live thread
below its candidate window and published it as ``failed``. The rollout log the
desktop appends to is the only witness, so liveness comes from there.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codexbridge.codex.app_server import CodexAppServerSource, thread_snapshot
from codexbridge.codex.collector import records_from_snapshot
from codexbridge.codex.rollout_activity import (
    RolloutActivity,
    read_log_tail,
    status_from_activity,
)
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub


def _stamp(offset_seconds: float) -> str:
    moment = datetime.fromtimestamp(time.time() + offset_seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def _event(kind: str, offset: float, **payload) -> str:
    return json.dumps(
        {"timestamp": _stamp(offset), "type": "event_msg", "payload": {"type": kind, **payload}}
    )


def _resume(offset: float) -> str:
    """What a resume or a detached read appends: a fresh stamp, no turn work."""
    return _event("thread_settings_applied", offset)


def _note(offset: float) -> str:
    """A record that reflects real work but is not a turn boundary."""
    return json.dumps(
        {"timestamp": _stamp(offset), "type": "response_item",
         "payload": {"type": "reasoning", "summary": []}}
    )


def _write_rollout(sessions_root: Path, thread_id: str, lines: list[str]) -> Path:
    day = datetime.now()
    folder = sessions_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TailReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _activity(self, *lines: str):
        path = self.root / "rollout-log.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return read_log_tail(path)

    def test_fresh_turn_record_is_working(self) -> None:
        self.assertEqual(
            status_from_activity(self._activity(_resume(-120), _event("task_started", -30), _note(-2))),
            "working",
        )

    def test_resume_after_a_completion_does_not_look_like_work(self) -> None:
        activity = self._activity(
            _event("task_started", -6_000), _event("task_complete", -5_000), _resume(-1)
        )
        self.assertEqual(status_from_activity(activity), "completed")

    def test_completion_then_new_turn_is_working_again(self) -> None:
        activity = self._activity(
            _event("task_complete", -900), _event("task_started", -30), _note(-5)
        )
        self.assertEqual(status_from_activity(activity), "working")

    def test_abort_settles_the_thread(self) -> None:
        activity = self._activity(_event("task_started", -400), _event("turn_aborted", -20))
        self.assertEqual(status_from_activity(activity), "idle")

    def test_a_failing_completion_is_not_reported_working(self) -> None:
        activity = self._activity(
            _event("task_started", -6_000),
            _event("task_complete", -5_000, error={"message": "boom"}),
            _resume(-1),
        )
        self.assertNotEqual(status_from_activity(activity), "working")

    def test_stale_turn_activity_settles_down(self) -> None:
        self.assertEqual(status_from_activity(self._activity(_event("task_started", -4_000), _note(-3_900))), "")

    def test_unreadable_log_is_not_working(self) -> None:
        self.assertIsNone(read_log_tail(self.root / "missing.jsonl"))
        self.assertEqual(status_from_activity(None), "")


class RolloutActivityTests(unittest.TestCase):
    def test_find_path_uses_the_session_day_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = _write_rollout(root, "thread-42", [_event("task_started", -10)])
            activity = RolloutActivity(root)
            self.assertEqual(activity.find_path("thread-42"), log)
            self.assertEqual(activity.activity_for_thread("thread-42")[1], "task_started")
            self.assertIsNone(activity.find_path("thread-99"))

    def test_activity_prefers_the_listing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            outside = Path(temp) / "elsewhere.jsonl"
            outside.write_text(_event("task_started", -10) + "\n", encoding="utf-8")
            activity = RolloutActivity(Path(temp) / "sessions")
            self.assertTrue(activity.is_working({"id": "whatever", "path": str(outside)}))

    def test_missing_activity_is_not_working(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            activity = RolloutActivity(Path(temp))
            self.assertFalse(activity.is_working({"id": "absent", "path": None}))


LIVE = "01a10000-0000-7000-8000-00000000000a"
STARTED = int(time.time()) - 17_000


def _listed(thread_id: str, recency_age: float) -> dict:
    return {
        "id": thread_id,
        "preview": f"Task {thread_id}",
        "recencyAt": int(time.time() - recency_age),
        "updatedAt": int(time.time() - recency_age),
        "status": {"type": "notLoaded"},
        "source": "vscode",
        "turns": [],
    }


def _turn(thread_id: str, running: bool) -> dict:
    if running:
        # Exactly how a detached app-server sees a live desktop turn.
        return {"id": "turn-1", "status": "interrupted", "startedAt": STARTED,
                "completedAt": None, "items": []}
    return {"id": "turn-0", "status": "completed", "startedAt": STARTED - 900,
            "completedAt": STARTED - 800, "items": []}


def _turn_summary(thread_id: str, running: bool) -> dict:
    """Turn metadata as ``thread/turns/list`` returns it: no items, no usage."""
    return {key: value for key, value in _turn(thread_id, running).items()
            if key != "items"}


def _thread_metadata(thread_id: str, running: bool) -> dict:
    """What ``thread/read {includeTurns: false}`` returns for one thread."""
    return {
        "thread": {
            "id": thread_id,
            "name": f"Task {thread_id}",
            "status": {"type": "notLoaded"},
            "source": "vscode",
            "recencyAt": STARTED,
            "updatedAt": int(time.time()),
            "turns": [],
        }
    }


def _turn_items(thread_id: str, running: bool, turn_id: str,
                sort_direction: str) -> dict:
    """One page of ``thread/items/list`` entries for a single turn."""
    items = [
        {"id": "item-user", "type": "userMessage",
         "content": [{"type": "text", "text": "hi"}]},
        {"id": "item-agent", "type": "agentMessage",
         "phase": "finalAnswer", "text": "done"},
    ]
    if sort_direction == "desc":
        items = list(reversed(items))
    return {"data": [{"item": item, "turnId": turn_id} for item in items]}


class CollectorLivenessTests(unittest.TestCase):
    """The collector must publish a desktop thread the listing calls dead."""

    def _source(
        self,
        listed: list[dict],
        activity: RolloutActivity,
        running_ids: tuple[str, ...] = (LIVE,),
        calls: list[str] | None = None,
    ) -> CodexAppServerSource:
        running = {str(item["id"]) for item in listed if item.get("id") in running_ids}

        class FakeClient:
            def request(self, method, params):
                if calls is not None:
                    calls.append(method)
                if method == "thread/list":
                    return {"data": listed}
                thread_id = str(params.get("threadId") or "")
                is_running = thread_id in running
                if method == "thread/read":
                    if params.get("includeTurns") is not False:
                        raise AssertionError(
                            "the collector must not read a whole thread")
                    return _thread_metadata(thread_id, is_running)
                if method == "thread/turns/list":
                    if params.get("itemsView") != "summary":
                        raise AssertionError("turn listings must stay summary-only")
                    # Newest-first, exactly like the app-server answers.
                    return {"data": [_turn_summary(thread_id, is_running)]}
                if method == "thread/items/list":
                    turn_id = str(params.get("turnId") or "")
                    return _turn_items(thread_id, is_running, turn_id,
                                       str(params.get("sortDirection") or "asc"))
                raise AssertionError(method)

            def close(self):
                pass

        source = CodexAppServerSource.__new__(CodexAppServerSource)
        source.runtime_folder = Path(".")
        source.cwd = Path(".")
        source.account = {}
        source.result = thread_snapshot(None)
        source.task = thread_snapshot(None)
        source.tasks = [source.task]
        source.active_thread_ids = set()
        source.client = FakeClient()
        source.activity = activity
        return source

    def test_live_thread_beats_a_stale_detached_reading(self) -> None:
        # Nine finished threads sit above the live one in recency order, which is
        # what pushed it out of the candidate window on hardware.
        listed = [_listed(f"01a10000-0000-7000-8000-0000000000{i:02d}", 400 * (i + 1))
                  for i in range(9)]
        listed.append(_listed(LIVE, 17_000))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_rollout(root, LIVE, [_event("task_started", -60), _note(-3)])
            for item in listed[:9]:
                _write_rollout(root, str(item["id"]),
                               [_event("task_started", -6_000), _event("task_complete", -5_900)])
            source = self._source(listed, RolloutActivity(root))
            source.refresh_thread()

        self.assertEqual(source.task["thread_id"], LIVE)
        self.assertEqual(source.task["status"], "working")
        self.assertIn(LIVE, {str(item["thread_id"]) for item in source.tasks})

    def test_finished_thread_is_not_revived(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_rollout(root, LIVE, [_event("task_started", -6_000), _event("task_complete", -5_900)])
            source = self._source(
                [_listed(LIVE, 400)], RolloutActivity(root), running_ids=()
            )
            source.refresh_thread()

        self.assertNotEqual(source.task["status"], "working")

    def test_a_resume_touching_the_log_does_not_fake_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_rollout(root, LIVE, [_event("task_complete", -5_900), _resume(-1)])
            source = self._source(
                [_listed(LIVE, 400)], RolloutActivity(root), running_ids=()
            )
            source.refresh_thread()

        self.assertNotEqual(source.task["status"], "working")

    def test_an_active_thread_is_not_reread_while_its_log_is_unchanged(self) -> None:
        # The expensive case: a thread that still looked "active" used to be
        # re-read on every 5 s beat, and each read pulls the whole stored
        # history (measured at ~134 MB on a long thread). An unchanged rollout
        # log proves the body cannot have grown, so the read is skipped.
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _write_rollout(root, LIVE, [_event("task_started", -30), _note(-3)])
            source = self._source(
                [_listed(LIVE, 400)],
                RolloutActivity(root),
                running_ids=(LIVE,),
                calls=calls,
            )
            source.refresh_thread()
            source.refresh_thread()

        self.assertEqual(calls.count("thread/read"), 1)

    def test_the_same_running_turn_skips_the_whole_thread_listing(self) -> None:
        # thread/turns/list makes the app server load the entire stored thread
        # (measured 134 MB cold on a long one). While the rollout log still
        # reports the same turn id, only the bounded item page is needed.
        calls: list[str] = []
        turn = "turn-1"  # matches the fake app-server summary
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _write_rollout(root, LIVE, [
                _event("task_started", -30, turn_id=turn),
                _note(-3),
            ])
            source = self._source(
                [_listed(LIVE, 400)],
                RolloutActivity(root),
                running_ids=(LIVE,),
                calls=calls,
            )
            source.refresh_thread()
            first_turns = calls.count("thread/turns/list")
            first_items = calls.count("thread/items/list")
            # Same turn keeps streaming: the log grows, the turn id does not.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_event("agent_message", -1, turn_id=turn) + "\n")
            source.refresh_thread()

        self.assertEqual(first_turns, 1)
        self.assertEqual(calls.count("thread/turns/list"), 1,
                         "the same running turn must not re-list every thread")
        self.assertGreater(calls.count("thread/items/list"), first_items,
                           "the item page must still refresh")

    def test_a_new_turn_re_reads_the_turn_list(self) -> None:
        calls: list[str] = []
        first = "turn-1"
        second = "turn-2"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _write_rollout(root, LIVE, [_event("task_started", -30, turn_id=first)])
            source = self._source(
                [_listed(LIVE, 400)],
                RolloutActivity(root),
                running_ids=(LIVE,),
                calls=calls,
            )
            source.refresh_thread()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_event("task_started", -1, turn_id=second) + "\n")
            source.refresh_thread()

        self.assertEqual(calls.count("thread/turns/list"), 2)

    def test_an_active_thread_is_reread_when_its_log_grows(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _write_rollout(root, LIVE, [_event("task_started", -30)])
            source = self._source(
                [_listed(LIVE, 400)],
                RolloutActivity(root),
                running_ids=(LIVE,),
                calls=calls,
            )
            source.refresh_thread()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_note(-1) + "\n")
            source.refresh_thread()

        self.assertEqual(calls.count("thread/read"), 2)

    def test_unchanged_idle_thread_is_not_reread_on_every_poll(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as temp:
            source = self._source(
                [_listed(LIVE, 400)],
                RolloutActivity(Path(temp)),
                running_ids=(),
                calls=calls,
            )
            source.refresh_thread()
            source.refresh_thread()

        self.assertEqual(calls.count("thread/read"), 1)


class RunningCountsTests(unittest.TestCase):
    def test_all_listed_inflight_threads_contribute_to_running_count(self) -> None:
        threads = [f"01a20000-0000-7000-8000-0000000000{i:02d}" for i in range(7)]
        listed = [_listed(thread_id, 400 * (index + 1))
                  for index, thread_id in enumerate(threads)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for thread_id in threads:
                _write_rollout(root, thread_id, [_event("task_started", -30)])
            source = CollectorLivenessTests()._source(
                listed,
                RolloutActivity(root),
                running_ids=tuple(threads),
            )
            source.refresh_thread()
            snapshot = {
                "generated_at_epoch": int(time.time()),
                "account": {},
                "task": source.task,
                "tasks": source.tasks,
                "result": source.result,
            }
            hub = BridgeHub(DeviceRegistry(Path(temp) / "devices.json"))
            for item in records_from_snapshot(snapshot):
                hub.publish(item)

        self.assertEqual(7, len(source.tasks))
        self.assertEqual(7, hub.counts()["running"])

    def test_queued_turn_status_is_preserved(self) -> None:
        snapshot = thread_snapshot({
            "id": "queued-thread",
            "name": "Queued",
            "source": "vscode",
            "recencyAt": int(time.time()),
            "turns": [{
                "id": "turn-q",
                "status": "queued",
                "startedAt": int(time.time()),
                "items": [],
            }],
        })
        self.assertEqual("queued", snapshot["status"])

    def test_a_live_task_record_counts_as_running(self) -> None:
        snapshot = {
            "generated_at_epoch": int(time.time()),
            "account": {},
            "task": {
                "thread_id": LIVE,
                "turn_id": "turn-1",
                "status": "working",
                "title": "Task",
                "last_user_message": "",
                "last_assistant_message": "",
                "codex_messages": [],
                "current_action": "\u6b63\u5728\u5904\u7406\u4efb\u52a1",
                "started_at_epoch": STARTED,
                "last_updated_epoch": int(time.time()),
                "completed_at_epoch": 0,
                "plan_completed": 0,
                "plan_total": 0,
            },
            "tasks": [],
            "result": {"thread_id": "", "status": "idle"},
        }
        with tempfile.TemporaryDirectory() as temp:
            hub = BridgeHub(DeviceRegistry(Path(temp) / "devices.json"))
            for record in records_from_snapshot(snapshot):
                hub.publish(record)
            self.assertEqual(hub.counts()["running"], 1)


if __name__ == "__main__":
    unittest.main()
