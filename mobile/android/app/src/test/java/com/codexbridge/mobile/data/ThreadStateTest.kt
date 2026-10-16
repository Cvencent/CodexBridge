package com.codexbridge.mobile.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

private class FakeCompletionState : CompletionNotificationState {
    val watermarks = mutableMapOf<String, Long>()
    val seenKeys = mutableSetOf<String>()

    override fun completionWatermark(threadId: String): Long = watermarks[threadId] ?: 0L

    override fun advanceCompletionWatermark(threadId: String, completedAt: Long) {
        if (completedAt > completionWatermark(threadId)) {
            watermarks[threadId] = completedAt
        }
    }

    override fun markCompletionSeen(threadId: String, notificationKey: String): Boolean =
        seenKeys.add(notificationKey)
}

class ThreadStateTest {
    @Test
    fun runningTaskIsNotReplacedByAStaleDetailFailure() {
        assertEquals("working", coherentTaskStatus("working", "failed"))
        assertEquals("waiting_user", coherentTaskStatus("waiting_user", "failed"))
        assertEquals("working", coherentTaskStatus("idle", "working"))
        assertEquals("idle", coherentTaskStatus("failed", "idle"))
    }

    @Test
    fun completedDetailStateRemainsCoherent() {
        assertEquals("completed", coherentTaskStatus("completed", "completed"))
        assertEquals("completed", coherentTaskStatus("idle", "completed"))
        assertEquals("completed", coherentTaskStatus("working", "completed"))
        assertEquals("working", coherentTaskStatus("working", "failed"))
    }

    @Test
    fun threadStatusMapsOntoTaskVocabulary() {
        assertEquals("working", threadStatusToTaskStatus("active", emptyList(), "idle"))
        assertEquals("working", threadStatusToTaskStatus("unknown", listOf("waiting"), "idle"))
        assertEquals("idle", threadStatusToTaskStatus("idle", emptyList(), "working"))
        assertEquals("completed", threadStatusToTaskStatus("completed", emptyList(), "working"))
        assertEquals("failed", threadStatusToTaskStatus("failed", emptyList(), "working"))
    }

    @Test
    fun staleFailedStatusEventCannotOverrideAnActiveThread() {
        assertEquals("working", applyThreadStatusEvent("working", "failed", emptyList()))
        assertEquals("waiting_user", applyThreadStatusEvent("waiting_user", "failed", emptyList()))
        assertEquals("working", applyThreadStatusEvent("working", "active", emptyList()))
    }

    @Test
    fun completionNotificationKeyIsSharedBySseAndPollingWhenTurnIsKnown() {
        val fromSse = completionNotificationKey("thread-1", "turn-1", 1000)
        val fromPolling = completionNotificationKey("thread-1", "turn-1", 1001)
        assertEquals(fromSse, fromPolling)
        assertTrue(fromSse != completionNotificationKey("thread-1", "turn-2", 1000))
        assertTrue(fromSse != completionNotificationKey("thread-2", "turn-1", 1000))
    }

    @Test
    fun completionNotificationKeyFallsBackToCompletionTime() {
        val first = completionNotificationKey("thread-1", "", 1000)
        assertEquals(first, completionNotificationKey("thread-1", "", 1000))
        assertTrue(first != completionNotificationKey("thread-1", "", 1001))
        assertTrue(first != completionNotificationKey("thread-2", "", 1000))
        assertEquals("thread-1", completionNotificationKey("thread-1", "", 0))
    }

    @Test
    fun historicalCompletionSnapshotOnlySeedsTheWatermark() {
        val state = FakeCompletionState()
        val tracker = CompletionNotificationTracker(state)
        tracker.seed(
            listOf(
                ThreadSummary(
                    thread_id = "thread-1",
                    turn_id = "turn-1",
                    status = "completed",
                    completed_at = 1000,
                ),
            ),
            beforeOrAt = 2000,
        )

        assertFalse(tracker.shouldNotify("thread-1", "turn-1", 0))
        assertFalse(tracker.shouldNotify("thread-1", "turn-1", 1000))
    }

    @Test
    fun sseAndPollingNotifyOneCompletionOnlyOnce() {
        val state = FakeCompletionState()
        val tracker = CompletionNotificationTracker(state)
        tracker.seed(
            listOf(ThreadSummary(thread_id = "thread-1", status = "working")),
            beforeOrAt = 2000,
        )

        assertTrue(tracker.shouldNotify("thread-1", "turn-2", 0))
        assertFalse(tracker.shouldNotify("thread-1", "turn-2", 2001))
        assertFalse(tracker.shouldNotify("thread-1", "turn-2", 2001))
    }

    @Test
    fun visibleThreadsKeepActiveTasksFirst() {
        val active = ThreadSummary(thread_id = "active", status = "working", recency_at = 10)
        val completed = ThreadSummary(thread_id = "completed", status = "completed", recency_at = 20)
        val idle = ThreadSummary(thread_id = "idle", status = "idle", recency_at = 30)

        val sorted = sortThreadsByActivity(listOf(completed, idle, active))

        assertEquals(listOf("active", "idle", "completed"), sorted.map { it.thread_id })
    }
}
