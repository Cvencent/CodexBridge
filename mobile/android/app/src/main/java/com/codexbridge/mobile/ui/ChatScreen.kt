package com.codexbridge.mobile.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import kotlinx.coroutines.launch
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.codexbridge.mobile.UiState
import com.codexbridge.mobile.data.ApprovalRequest
import com.codexbridge.mobile.data.MessageItem
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    state: UiState,
    onBack: () -> Unit,
    onSend: (String) -> Unit,
    onStop: () -> Unit,
    onPickModel: (String) -> Unit,
    onResolveApproval: (Int, String) -> Unit,
) {
    var input by remember { mutableStateOf("") }
    var showModels by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val threadKey = state.activeThread?.thread_id.orEmpty()
    val itemCount = state.messages.size + state.approvals.size
    var lastBottomId by remember(threadKey) { mutableStateOf("") }
    var userScrolledUp by remember(threadKey) { mutableStateOf(false) }
    val pinnedToBottom by remember {
        derivedStateOf {
            val layout = listState.layoutInfo
            val last = layout.visibleItemsInfo.lastOrNull()
            last == null || last.index >= layout.totalItemsCount - 1 ||
                listState.canScrollForward.not()
        }
    }
    val showJumpToBottom by remember {
        derivedStateOf {
            listState.canScrollBackward && (!pinnedToBottom || userScrolledUp)
        }
    }

    // Opening a thread jumps straight to the newest message.
    LaunchedEffect(threadKey) {
        if (threadKey.isNotBlank()) {
            listState.scrollToItem(0)
            if (itemCount > 0) listState.scrollToItem(itemCount - 1)
        }
    }
    // A newly loaded message scrolls to the bottom instantly; deltas only
    // follow while the user is already reading the bottom of the list.
    LaunchedEffect(threadKey, itemCount) {
        val lastId = state.messages.lastOrNull()?.id
            ?: state.approvals.lastOrNull()?.let { "approval-" + it.request_id }
            ?: ""
        if (lastId.isNotBlank() && lastId != lastBottomId) {
            lastBottomId = lastId
            listState.scrollToItem(itemCount - 1)
        }
    }
    LaunchedEffect(threadKey, state.streaming.size, state.working, state.task.current_action) {
        if (itemCount > 0 && pinnedToBottom) {
            listState.scrollToItem(itemCount - 1)
        }
    }
    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            text = state.activeThread?.title?.ifBlank { "Codex 任务" } ?: "Codex 任务",
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            fontWeight = FontWeight.Medium,
                        )
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                text = statusLabel(state.task.status.ifBlank { state.activeThread?.status ?: "idle" }),
                                fontSize = 12.sp,
                                color = statusColor(state.task.status.ifBlank { state.activeThread?.status ?: "idle" }),
                            )
                            if (state.task.tokenCount > 0) {
                                Text(
                                    text = " · ${formatTokens(state.task.tokenCount)} tokens",
                                    fontSize = 12.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "返回")
                    }
                },
                actions = {
                    AssistChip(
                        onClick = { showModels = true },
                        label = { Text(state.modelLabel, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                    )
                    if (state.working) {
                        IconButton(onClick = onStop) {
                            Icon(Icons.Filled.Close, contentDescription = "中断", tint = MaterialTheme.colorScheme.error)
                        }
                    }
                },
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding).imePadding()) {
            state.error?.let { message ->
                Text(
                    text = message,
                    color = MaterialTheme.colorScheme.error,
                    fontSize = 13.sp,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp),
                )
            }
            Box(modifier = Modifier.weight(1f).fillMaxWidth()) {
                LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(horizontal = 14.dp, vertical = 10.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    items(state.messages, key = { it.id.ifBlank { it.hashCode().toString() } }) { item ->
                        MessageRow(item = item, streaming = state.streaming[item.id].orEmpty())
                    }
                    items(state.approvals, key = { "approval-" + it.request_id }) { approval ->
                        ApprovalCard(approval = approval, onDecision = onResolveApproval)
                    }
                }
                if (showJumpToBottom) {
                    Surface(
                        modifier = Modifier
                            .align(Alignment.BottomCenter)
                            .padding(bottom = 10.dp)
                            .clickable {
                                val target = state.messages.size + state.approvals.size
                                if (target > 0) scope.launch { listState.scrollToItem(target - 1) }
                            },
                        shape = RoundedCornerShape(999.dp),
                        color = MaterialTheme.colorScheme.surfaceVariant,
                        tonalElevation = 4.dp,
                    ) {
                        Text(
                            text = "回到底部",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                        )
                    }
                }
            }
            if (state.working) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Box(
                        modifier = Modifier
                            .size(7.dp)
                            .background(MaterialTheme.colorScheme.secondary, RoundedCornerShape(999.dp)),
                    )
                    Spacer(modifier = Modifier.width(8.dp))
                    Text(
                        text = state.task.currentActionOrFallback(),
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
            }
            Row(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                verticalAlignment = Alignment.Bottom,
            ) {
                OutlinedTextField(
                    value = input,
                    onValueChange = { input = it },
                    placeholder = {
                        Text(if (state.working) "发送将追加到当前任务" else "给 Codex 发消息…", maxLines = 1)
                    },
                    maxLines = 5,
                    shape = RoundedCornerShape(16.dp),
                    modifier = Modifier.weight(1f),
                )
                Spacer(modifier = Modifier.width(8.dp))
                IconButton(
                    onClick = {
                        val text = input.trim()
                        if (text.isNotEmpty()) {
                            input = ""
                            onSend(text)
                        }
                    },
                    enabled = input.isNotBlank(),
                    modifier = Modifier
                        .size(48.dp)
                        .background(MaterialTheme.colorScheme.primary, RoundedCornerShape(16.dp)),
                ) {
                    Icon(
                        Icons.Filled.Send,
                        contentDescription = "发送",
                        tint = MaterialTheme.colorScheme.onPrimary,
                    )
                }
            }
        }
    }

    if (showModels) {
        ModelPickerSheet(
            models = state.models,
            selectedModel = state.selectedModel,
            onSelect = { model ->
                showModels = false
                onPickModel(model.slug)
            },
            onDismiss = { showModels = false },
        )
    }
}

private fun com.codexbridge.mobile.data.TaskSnapshot.currentActionOrFallback(): String =
    current_action.ifBlank { if (status == "waiting_user") "等待你的确认" else "Codex 正在处理…" }

@Composable
private fun MessageRow(item: MessageItem, streaming: String) {
    when (item.kind) {
        "user" -> UserBubble(item.text)
        "agent" -> AgentBubble(item = item, streaming = streaming)
        "commentary" -> QuietBlock(text = item.text, label = null, italic = true)
        "reasoning" -> QuietBlock(text = item.text, label = "思考", italic = false)
        "command" -> CommandBlock(item)
        "fileChange" -> FileChangeBlock(item)
        "tool" -> ToolBlock(item)
        "plan" -> PlanBlock(item)
        else -> NoticeLine(item.text.ifBlank { item.detail.message })
    }
}

@Composable
private fun UserBubble(text: String) {
    if (text.isBlank()) return
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Box(
            modifier = Modifier
                .widthIn(max = 320.dp)
                .background(
                    MaterialTheme.colorScheme.primaryContainer,
                    RoundedCornerShape(16.dp, 16.dp, 4.dp, 16.dp),
                )
                .padding(horizontal = 12.dp, vertical = 9.dp),
        ) {
            Text(
                text = text,
                fontSize = 14.sp,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
            )
        }
    }
}

@Composable
private fun AgentBubble(item: MessageItem, streaming: String) {
    val live = streaming.isNotEmpty() && item.text.isBlank()
    val body = item.text.ifBlank { streaming }
    if (body.isBlank()) return
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
        Card(
            modifier = Modifier.widthIn(max = 340.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            shape = RoundedCornerShape(4.dp, 16.dp, 16.dp, 16.dp),
        ) {
            Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        text = "Codex",
                        fontSize = 11.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.secondary,
                    )
                    if (live) {
                        Spacer(modifier = Modifier.width(6.dp))
                        Text(
                            text = "正在输出",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                Spacer(modifier = Modifier.height(4.dp))
                Text(text = body, fontSize = 14.sp)
            }
        }
    }
}

@Composable
private fun QuietBlock(text: String, label: String?, italic: Boolean) {
    if (text.isBlank()) return
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
        Column(modifier = Modifier.widthIn(max = 340.dp).padding(start = 4.dp)) {
            if (label != null) {
                Text(
                    text = label,
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontWeight = FontWeight.Medium,
                )
                Spacer(modifier = Modifier.height(2.dp))
            }
            Text(
                text = text,
                fontSize = 13.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                fontStyle = if (italic) FontStyle.Italic else FontStyle.Normal,
            )
        }
    }
}

@Composable
private fun CommandBlock(item: MessageItem) {
    val detail = item.detail
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = "命令",
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                val footer = commandFooter(detail.status, detail.exitCode, detail.durationMs)
                if (footer.isNotBlank()) {
                    Text(text = footer, fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            val command = item.text.ifBlank { detail.message }
            if (command.isNotBlank()) {
                MonoBlock("$ " + command.trim())
            }
            if (detail.cwd.isNotBlank()) {
                Text(
                    text = detail.cwd,
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            if (detail.output.isNotBlank()) {
                MonoBlock(detail.output.take(6000))
            }
        }
    }
}

@Composable
private fun FileChangeBlock(item: MessageItem) {
    val detail = item.detail
    val diff = detail.diff.ifBlank { detail.patch }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                text = "文件改动",
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            detail.paths.forEach { path ->
                Text(
                    text = path,
                    fontFamily = FontFamily.Monospace,
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurface,
                )
            }
            if (diff.isNotBlank()) {
                MonoBlock(diff.take(6000))
            } else if (item.text.isNotBlank() || detail.message.isNotBlank()) {
                Text(
                    text = item.text.ifBlank { detail.message },
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun ToolBlock(item: MessageItem) {
    val detail = item.detail
    val body = item.text.ifBlank { detail.message }
    val result = detail.result.ifBlank { detail.output }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                text = "工具调用",
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (body.isNotBlank()) {
                Text(text = body, fontSize = 13.sp)
            }
            if (result.isNotBlank()) {
                MonoBlock(result.take(6000))
            }
        }
    }
}

@Composable
private fun PlanBlock(item: MessageItem) {
    val steps = item.detail.steps
    if (steps.isEmpty() && item.text.isBlank()) return
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(
            modifier = Modifier.padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Text(
                text = "计划",
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (steps.isEmpty()) {
                Text(text = item.text, fontSize = 13.sp)
            }
            steps.forEach { step ->
                val done = step.status == "completed"
                val active = step.status == "in_progress" || step.status == "inProgress"
                val marker = when {
                    done -> "✓"
                    active -> "◐"
                    else -> "○"
                }
                val color = when {
                    done -> MaterialTheme.colorScheme.secondary
                    active -> MaterialTheme.colorScheme.tertiary
                    else -> MaterialTheme.colorScheme.onSurfaceVariant
                }
                Row(verticalAlignment = Alignment.Top) {
                    Text(text = marker, fontSize = 13.sp, color = color, modifier = Modifier.width(18.dp))
                    Text(
                        text = step.step,
                        fontSize = 13.sp,
                        color = if (done) MaterialTheme.colorScheme.onSurfaceVariant else color,
                    )
                }
            }
        }
    }
}

@Composable
private fun NoticeLine(text: String) {
    if (text.isBlank()) return
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.Center,
    ) {
        Text(
            text = text,
            fontSize = 12.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}

@Composable
private fun ApprovalCard(approval: ApprovalRequest, onDecision: (Int, String) -> Unit) {
    val detail = approval.raw
    val diff = detail.diff.ifBlank { detail.patch }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.error.copy(alpha = 0.45f)),
    ) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    imageVector = Icons.Filled.Warning,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.error,
                    modifier = Modifier.size(18.dp),
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = approval.title,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.weight(1f),
                )
            }
            if (approval.command.isNotBlank()) {
                MonoBlock("$ " + approval.command.trim())
            }
            if (detail.paths.isNotEmpty()) {
                Text(
                    text = detail.paths.joinToString("\n"),
                    fontFamily = FontFamily.Monospace,
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (diff.isNotBlank()) {
                MonoBlock(diff.take(4000))
            }
            detail.questions.forEach { question ->
                Column {
                    if (question.question.isNotBlank()) {
                        Text(text = question.question, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                    }
                    if (question.options.isNotEmpty()) {
                        Text(
                            text = question.options.joinToString(" / "),
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
            if (approval.reason.isNotBlank()) {
                Text(
                    text = approval.reason,
                    fontSize = 13.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (
                approval.command.isBlank() &&
                detail.paths.isEmpty() &&
                diff.isBlank() &&
                detail.questions.isEmpty() &&
                approval.reason.isBlank() &&
                detail.message.isNotBlank()
            ) {
                Text(text = detail.message, fontSize = 13.sp)
            }
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Button(
                    onClick = { onDecision(approval.request_id, "accept") },
                    modifier = Modifier.weight(1f),
                ) {
                    Text("允许")
                }
                OutlinedButton(
                    onClick = { onDecision(approval.request_id, "acceptForSession") },
                    modifier = Modifier.weight(1f),
                ) {
                    Text("本会话允许")
                }
            }
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                TextButton(
                    onClick = { onDecision(approval.request_id, "decline") },
                    modifier = Modifier.weight(1f),
                ) {
                    Text("拒绝")
                }
                TextButton(
                    onClick = { onDecision(approval.request_id, "cancel") },
                    modifier = Modifier.weight(1f),
                ) {
                    Text("取消")
                }
            }
        }
    }
}

private fun commandFooter(status: String, exitCode: Int?, durationMs: Long?): String {
    val parts = mutableListOf<String>()
    if (status.isNotBlank()) parts += status
    if (exitCode != null) parts += if (exitCode == 0) "退出码 0" else "退出码 $exitCode"
    val duration = formatDuration(durationMs)
    if (duration.isNotBlank()) parts += duration
    return parts.joinToString(" · ")
}

private fun formatDuration(ms: Long?): String {
    if (ms == null || ms <= 0) return ""
    return when {
        ms < 1000 -> "${ms}ms"
        ms < 60_000 -> String.format(Locale.US, "%.1fs", ms / 1000.0)
        else -> "${ms / 60_000}m${(ms % 60_000) / 1000}s"
    }
}