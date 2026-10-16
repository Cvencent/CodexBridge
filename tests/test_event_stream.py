import unittest
from codexbridge.companion.events import EventStream


class EventStreamTests(unittest.TestCase):
    def test_resume_replays_and_then_delivers_live_events(self):
        events = EventStream(capacity=3)
        first = events.publish({'type': 'turn_started'})
        second = events.publish({'type': 'turn_completed'})
        target = events.subscribe(first['event_id'])
        self.assertEqual(target.get_nowait(), second)
        third = events.publish({'type': 'threads_changed'})
        self.assertEqual(target.get_nowait(), third)
        self.assertNotEqual(first['event_id'], second['event_id'])

    def test_old_or_other_process_cursor_requires_snapshot(self):
        events = EventStream(capacity=1)
        first = events.publish({'type': 'turn_started'})
        events.publish({'type': 'turn_completed'})
        for cursor in [first['event_id'], 'another-process:1']:
            target = events.subscribe(cursor)
            self.assertEqual(target.get_nowait()['type'], 'resync_required')

    def test_subscriber_overflow_requests_resync_instead_of_silent_loss(self):
        events = EventStream(capacity=3, subscriber_capacity=1)
        target = events.subscribe()
        events.publish({'type': 'turn_started'})
        events.publish({'type': 'turn_completed'})
        self.assertEqual(target.get_nowait()['type'], 'resync_required')

    def test_payload_changes_do_not_mutate_replay(self):
        events = EventStream()
        payload = {'type': 'item_completed', 'item': {'text': 'original'}}
        first = events.publish({'type': 'turn_started'})
        published = events.publish(payload)
        payload['item']['text'] = 'changed'
        published['item']['text'] = 'also changed'
        self.assertEqual(events.subscribe(first['event_id']).get_nowait()['item']['text'], 'original')
