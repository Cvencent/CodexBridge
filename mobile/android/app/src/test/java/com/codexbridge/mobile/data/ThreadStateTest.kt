package com.codexbridge.mobile.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

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
    fun completionNotificationKeyIsSharedBySseAndPolling() {
        val fromSse = completionNotificationKey("thread-1", "turn-1", 1000)
        val fromPolling = completionNotificationKey("thread-1", "", 1000)
        assertEquals(fromSse, fromPolling)
        assertTrue(fromSse != completionNotificationKey("thread-1", "", 1001))
        assertTrue(fromSse != completionNotificationKey("thread-2", "", 1000))
    }

    @Test
    fun completionNotificationKeyFallsBackToTurnId() {
        val first = completionNotificationKey("thread-1", "turn-1")
        assertEquals(first, completionNotificationKey("thread-1", "turn-1"))
        assertTrue(first != completionNotificationKey("thread-1", "turn-2"))
        assertTrue(first != completionNotificationKey("thread-2", "turn-1"))
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
