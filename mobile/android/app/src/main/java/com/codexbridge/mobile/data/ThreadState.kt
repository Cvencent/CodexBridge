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

val ACTIVE_TASK_STATUSES = setOf("working", "waiting_user")

/**
 * Keep one thread's UI state coherent while a detail request is in flight.
 *
 * Desktop turns can report a stale failed snapshot in a detail response even
 * while the live rollout says the turn is still running, which used to make the
 * phone flip from "working" to "failed" the moment a thread was opened. An
 * incoming active state always wins; a completed/idle result wins over a stale
 * active state; an incoming failure is ignored while the thread is still active.
 */
fun coherentTaskStatus(current: String, incoming: String): String {
    val left = current.lowercase()
    val right = incoming.lowercase()
    return when {
        right in ACTIVE_TASK_STATUSES -> right
        left in ACTIVE_TASK_STATUSES && right == "failed" -> left
        left in ACTIVE_TASK_STATUSES && (right == "completed" || right == "idle") -> right
        right.isNotBlank() -> right
        left.isNotBlank() -> left
        else -> right
    }
}

/** Map a `thread/status/changed` payload onto the task status vocabulary. */
fun threadStatusToTaskStatus(status: String, activeFlags: List<String>, current: String): String =
    when {
        status == "active" || activeFlags.isNotEmpty() -> "working"
        status == "idle" -> "idle"
        status == "completed" -> "completed"
        status == "failed" -> "failed"
        status == "notloaded" -> current.ifBlank { "idle" }
        else -> current
    }

fun applyThreadStatusEvent(current: String, status: String, activeFlags: List<String>): String =
    coherentTaskStatus(current, threadStatusToTaskStatus(status.lowercase(), activeFlags, current))

fun sortThreadsByActivity(threads: List<ThreadSummary>): List<ThreadSummary> =
    threads.sortedWith(
        compareBy<ThreadSummary> { if (it.status in ACTIVE_TASK_STATUSES) 0 else 1 }
            .thenByDescending { maxOf(it.recency_at, it.updated_at) }
            .thenByDescending { it.thread_id },
    )

fun completionNotificationKey(threadId: String, turnId: String, completedAt: Long = 0): String =
    if (completedAt > 0) {
        "$threadId:at-$completedAt"
    } else {
        "$threadId:turn-$turnId"
    }
