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

fun List<ThreadSummary>.withThreadTitle(threadId: String, title: String): List<ThreadSummary> =
    map { if (it.thread_id == threadId) it.copy(title = title) else it }

fun TaskSnapshot.withThreadTitle(threadId: String, title: String): TaskSnapshot =
    if (this.thread_id == threadId) copy(title = title) else this
fun mergeThreadSnapshots(
    current: List<ThreadSummary>,
    incoming: List<ThreadSummary>,
    activeThreadId: String?,
): List<ThreadSummary> {
    val incomingIds = incoming.mapTo(mutableSetOf()) { it.thread_id }
    val active = activeThreadId?.let { id -> current.firstOrNull { it.thread_id == id } }
    return if (active != null && active.thread_id !in incomingIds) {
        (listOf(active) + incoming).distinctBy { it.thread_id }
    } else {
        incoming.distinctBy { it.thread_id }
    }
}

fun workingAfterTurnEvent(
    current: Boolean,
    activeThreadId: String?,
    eventThreadId: String,
    running: Boolean,
): Boolean {
    if (eventThreadId.isBlank()) return running
    return if (eventThreadId == activeThreadId) running else current
}

fun completionNotificationKey(threadId: String, turnId: String): String =
    "$threadId:$turnId"

fun shouldReconcileCompletion(status: String, completedAt: Long, since: Long): Boolean =
    status == "completed" && completedAt > 0 && completedAt >= since

fun notificationResumeSince(saved: Long, now: Long): Long =
    if (saved in 1..now) saved else now
