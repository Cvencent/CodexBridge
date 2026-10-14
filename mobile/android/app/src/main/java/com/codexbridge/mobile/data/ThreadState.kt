package com.codexbridge.mobile.data

/**
 * A completion belongs to the phone only after the user has opened it once.
 * A newer completion timestamp makes the thread visible as completed again.
 */
fun resolveThreadStatus(status: String, completedAt: Long, completionSeenAt: Long): String {
    if (status == "completed" && completedAt > 0 && completedAt <= completionSeenAt) {
        return "idle"
    }
    return status
}

fun ThreadSummary.withCompletionSeen(completionSeenAt: Long): ThreadSummary =
    copy(status = resolveThreadStatus(status, completed_at, completionSeenAt))

fun completionNotificationKey(threadId: String, turnId: String): String =
    "$threadId:$turnId"
