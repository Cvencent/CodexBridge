package com.codexbridge.mobile.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ThreadStateTest {
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
}
