package com.codexbridge.mobile.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ThreadStateTest {
    @Test
    fun restartedListenerKeepsItsLastSuccessfulWatermark() {
        assertEquals(100L, notificationResumeSince(100, 200))
        assertEquals(200L, notificationResumeSince(0, 200))
        assertEquals(200L, notificationResumeSince(300, 200))
    }

    @Test
    fun reconnectOnlyAlertsForCompletionsSinceListenerStarted() {
        assertTrue(shouldReconcileCompletion("completed", 101, 100))
        assertTrue(!shouldReconcileCompletion("completed", 99, 100))
        assertTrue(!shouldReconcileCompletion("working", 101, 100))
    }

    @Test
    fun threadNameEventUpdatesSummaryListAndActiveTaskTogether() {
        val rows = listOf(ThreadSummary(thread_id = "t1", title = "新任务"), ThreadSummary(thread_id = "t2", title = "别的"))
        val updated = rows.withThreadTitle("t1", "修复同步")
        assertEquals("修复同步", updated.first { it.thread_id == "t1" }.title)
        assertEquals("别的", updated.first { it.thread_id == "t2" }.title)
        val task = TaskSnapshot(thread_id = "t1", title = "新任务").withThreadTitle("t1", "修复同步")
        assertEquals("修复同步", task.title)
    }

    @Test
    fun completedThreadBecomesIdleAfterItWasOpened() {
        assertEquals("idle", resolveThreadStatus("completed", 1000, 1000))
        assertEquals("idle", resolveThreadStatus("completed", 900, 1000))
    }

    @Test
    fun newerCompletionIsNotHiddenByAnOldSeenMarker() {
        assertEquals("completed", resolveThreadStatus("completed", 1001, 1000))
    }

    @Test
    fun nonCompletedStatusesAreUnchanged() {
        assertEquals("working", resolveThreadStatus("working", 1000, 1000))
        assertEquals("failed", resolveThreadStatus("failed", 1000, 1000))
    }

    @Test
    fun notificationKeyDistinguishesTurnAndThread() {
        val first = completionNotificationKey("thread-1", "turn-1")
        assertEquals(first, completionNotificationKey("thread-1", "turn-1"))
        assertTrue(first != completionNotificationKey("thread-1", "turn-2"))
        assertTrue(first != completionNotificationKey("thread-2", "turn-1"))
    }

    @Test
    fun listRefreshAdoptsServerStatusForActiveThread() {
        val current = listOf(ThreadSummary(thread_id = "t1", status = "idle", title = "旧标题"))
        val incoming = listOf(
            ThreadSummary(thread_id = "t1", status = "working", title = "新标题"),
            ThreadSummary(thread_id = "t2", status = "completed"),
        )

        val merged = mergeThreadSnapshots(current, incoming, activeThreadId = "t1")

        assertEquals(listOf("t1", "t2"), merged.map { it.thread_id })
        assertEquals("working", merged.first().status)
        assertEquals("新标题", merged.first().title)
    }

    @Test
    fun listRefreshKeepsActiveThreadWhenServerPageNoLongerContainsIt() {
        val current = listOf(ThreadSummary(thread_id = "old-active", status = "working"))
        val incoming = listOf(ThreadSummary(thread_id = "newer", status = "completed"))

        val merged = mergeThreadSnapshots(current, incoming, activeThreadId = "old-active")

        assertEquals(listOf("old-active", "newer"), merged.map { it.thread_id })
    }

    @Test
    fun unrelatedTurnEventsDoNotChangeTheOpenThreadWorkingState() {
        assertTrue(workingAfterTurnEvent(true, "open", "other", running = false))
        assertTrue(workingAfterTurnEvent(true, "open", "other", running = true))
        assertTrue(!workingAfterTurnEvent(false, "open", "open", running = false))
        assertTrue(workingAfterTurnEvent(false, "open", "open", running = true))
    }
}
