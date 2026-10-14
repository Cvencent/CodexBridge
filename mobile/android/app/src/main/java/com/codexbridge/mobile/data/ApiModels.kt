package com.codexbridge.mobile.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class PingResponse(
    val ok: Boolean = false,
    val service: String = "",
    val name: String = "",
    val version: String = "",
    val port: Int = 0,
    val discovery_port: Int = 0,
    val requires_pairing: Boolean = false,
)

@Serializable
data class BridgeInfo(
    val name: String = "",
    val version: String = "",
    val port: Int = 0,
)

@Serializable
data class PairResponse(
    val ok: Boolean = false,
    val phone_id: String = "",
    val token: String = "",
    val bridge: BridgeInfo = BridgeInfo(),
)

@Serializable
data class SendResponse(
    val ok: Boolean = false,
    val thread_id: String = "",
    val turn_id: String = "",
    val status: String = "",
)

@Serializable
data class CreateThreadResponse(
    val ok: Boolean = false,
    val thread: ThreadSummary = ThreadSummary(),
)

@Serializable
data class ActionResponse(
    val ok: Boolean = false,
    val resolved: Boolean = false,
    val removed: Boolean = false,
)

@Serializable
data class Account(
    val plan_type: String = "",
    val rate_limit_reached: String = "",
    val five_hour_available: Boolean = false,
    val five_hour_remaining_percent: Int = 0,
    val five_hour_reset_date: String = "",
    val weekly_available: Boolean = false,
    val weekly_remaining_percent: Int = 0,
    val weekly_used_percent: Int = 0,
    val reset_date: String = "",
    val weekly_tokens_available: Boolean = false,
    val weekly_tokens: Long = 0,
    val weekly_tokens_as_of: String = "",
)

@Serializable
data class ThreadSummary(
    val thread_id: String = "",
    val title: String = "",
    val preview: String = "",
    val status: String = "idle",
    val model: String = "",
    val cwd: String = "",
    val workspace: String = "",
    val source: String = "",
    val created_at: Long = 0,
    val updated_at: Long = 0,
    val recency_at: Long = 0,
    val completed_at: Long = 0,
    val tokens: Long = 0,
    val ephemeral: Boolean = false,
)

@Serializable
data class ThreadsResponse(
    val ok: Boolean = false,
    val threads: List<ThreadSummary> = emptyList(),
    val next_cursor: String? = null,
)

@Serializable
data class PlanStep(
    val step: String = "",
    val status: String = "",
)

@Serializable
data class ApprovalQuestion(
    val id: String = "",
    val question: String = "",
    val options: List<String> = emptyList(),
)

@Serializable
data class MessageDetail(
    val status: String = "",
    @SerialName("exit_code") val exitCode: Int? = null,
    val cwd: String = "",
    @SerialName("duration_ms") val durationMs: Long? = null,
    val output: String = "",
    val paths: List<String> = emptyList(),
    val diff: String = "",
    val result: String = "",
    val patch: String = "",
    val message: String = "",
    val steps: List<PlanStep> = emptyList(),
    val questions: List<ApprovalQuestion> = emptyList(),
)

@Serializable
data class MessageItem(
    val id: String = "",
    val kind: String = "",
    val text: String = "",
    val detail: MessageDetail = MessageDetail(),
    val turn_id: String = "",
    val created_at: Long = 0,
) {
    val isUser: Boolean get() = kind == "user"
    val isAssistant: Boolean get() = kind == "agent"
    val isCommentary: Boolean get() = kind == "commentary"
}

@Serializable
data class TaskSnapshot(
    val status: String = "",
    val thread_id: String = "",
    val turn_id: String = "",
    val title: String = "",
    val current_action: String = "",
    val last_user_message: String = "",
    val last_assistant_message: String = "",
    val conversation_mode: String = "",
    @SerialName("token_count") val tokenCount: Long = 0,
    @SerialName("token_count_available") val tokenCountAvailable: Boolean = false,
    val plan_completed: Int = 0,
    val plan_total: Int = 0,
    val started_at_epoch: Long = 0,
    val last_updated_epoch: Long = 0,
    val completed_at_epoch: Long = 0,
) {
    val isWorking: Boolean get() = status == "working" || status == "waiting_user"
}

@Serializable
data class ApprovalRequest(
    val request_id: Int = 0,
    val kind: String = "",
    val method: String = "",
    val thread_id: String = "",
    val turn_id: String = "",
    val item_id: String = "",
    val command: String = "",
    val cwd: String = "",
    val reason: String = "",
    val created_at: Double = 0.0,
    val expires_at: Double = 0.0,
    val raw: MessageDetail = MessageDetail(),
) {
    val title: String
        get() = when (kind) {
            "command" -> "请求执行命令"
            "fileChange" -> "请求修改文件"
            "permissions" -> "请求额外权限"
            "input" -> "需要你的输入"
            "mcp" -> "工具请求确认"
            else -> "需要确认"
        }
}

@Serializable
data class StatusResponse(
    val ok: Boolean = false,
    val generated_at_epoch: Long = 0,
    val account: Account = Account(),
    val task: TaskSnapshot = TaskSnapshot(),
    val tasks: List<TaskSnapshot> = emptyList(),
    val result: TaskSnapshot = TaskSnapshot(),
    val pending_approvals: List<ApprovalRequest> = emptyList(),
)

@Serializable
data class ThreadDetailResponse(
    val ok: Boolean = false,
    val thread: ThreadSummary = ThreadSummary(),
    val task: TaskSnapshot = TaskSnapshot(),
    val messages: List<MessageItem> = emptyList(),
    val message_count: Int = 0,
    val truncated: Boolean = false,
    val pending_approvals: List<ApprovalRequest> = emptyList(),
)

@Serializable
data class ModelEffort(
    val effort: String = "",
    val description: String = "",
)

@Serializable
data class ModelInfo(
    val id: String = "",
    val model: String = "",
    val display_name: String = "",
    val description: String = "",
    val is_default: Boolean = false,
    val default_effort: String = "",
    val efforts: List<ModelEffort> = emptyList(),
    val input_modalities: List<String> = emptyList(),
) {
    val label: String get() = display_name.ifBlank { model.ifBlank { id } }
    val slug: String get() = model.ifBlank { id }
}

@Serializable
data class ModelsResponse(
    val ok: Boolean = false,
    val models: List<ModelInfo> = emptyList(),
)

@Serializable
data class ProjectInfo(
    val project_id: String = "",
    val name: String = "",
    val cwd: String = "",
    val roots: List<String> = emptyList(),
    val updated_at: Long = 0,
)

@Serializable
data class ProjectsResponse(
    val ok: Boolean = false,
    val projects: List<ProjectInfo> = emptyList(),
    val default_cwd: String = "",
)

/** Parsed SSE payload from `/api/companion/events`. */
@Serializable
data class StreamEvent(
    val type: String = "",
    val thread_id: String = "",
    val turn_id: String = "",
    val item_id: String = "",
    val delta: String = "",
    val status: String = "",
    val title: String = "",
    val message: String = "",
    val decision: String = "",
    val request_id: Int = 0,
    val total_tokens: Long = 0,
    val context_window: Long = 0,
    val duration_ms: Long? = null,
    val active_flags: List<String> = emptyList(),
    val item: MessageItem? = null,
    // Approval payload fields.
    val kind: String = "",
    val command: String = "",
    val cwd: String = "",
    val reason: String = "",
    val expires_at: Double = 0.0,
    val created_at: Double = 0.0,
    val raw: MessageDetail = MessageDetail(),
)

@Serializable
data class ErrorEnvelope(
    val ok: Boolean = false,
    val error: String = "",
    val message: String = "",
)
