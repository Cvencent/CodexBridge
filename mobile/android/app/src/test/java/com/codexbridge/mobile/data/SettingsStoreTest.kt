package com.codexbridge.mobile.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class SettingsStoreTest {
    @Test
    fun pendingCompletionSurvivesStoreRecreationAndCanBeCleared() {
        val context = RuntimeEnvironment.getApplication()
        val first = SettingsStore(context)
        first.recordPendingCompletion("thread-1", "completed", 123)

        val reloaded = SettingsStore(context)
        assertEquals(
            listOf(PendingCompletion("thread-1", "completed", 123)),
            reloaded.pendingCompletions(),
        )

        reloaded.clearPendingCompletion("thread-1")
        assertTrue(SettingsStore(context).pendingCompletions().isEmpty())
    }
}
