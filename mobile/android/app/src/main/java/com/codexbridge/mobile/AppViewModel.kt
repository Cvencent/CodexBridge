package com.codexbridge.mobile

import android.app.Application
import android.content.Intent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.codexbridge.mobile.data.Account
import com.codexbridge.mobile.data.ApiClient
import com.codexbridge.mobile.data.ApiException
import com.codexbridge.mobile.data.ApprovalRequest
import com.codexbridge.mobile.data.DiscoveredBridge
import com.codexbridge.mobile.data.Discovery
import com.codexbridge.mobile.data.MessageDetail
import com.codexbridge.mobile.data.MessageItem
import com.codexbridge.mobile.data.ModelInfo
import com.codexbridge.mobile.data.ProjectInfo
import com.codexbridge.mobile.data.SettingsStore
import com.codexbridge.mobile.data.StreamEvent
import com.codexbridge.mobile.data.TaskSnapshot
import com.codexbridge.mobile.data.ThreadSummary
import com.codexbridge.mobile.data.withCompletionSeen
import com.codexbridge.mobile.data.withThreadTitle
import com.codexbridge.mobile.data.resolveThreadStatus
import com.codexbridge.mobile.data.mergeThreadSnapshots
import com.codexbridge.mobile.data.workingAfterTurnEvent
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID

enum class AppScreen { Pair, Threads, Chat, Settings }

data class UiState(
    val screen: AppScreen = AppScreen.Pair,
    val baseUrl: String = "",
    val bridgeName: String = "",
    val bridgeVersion: String = "",
    val phoneId: String = "",
    val paired: Boolean = false,
    val connected: Boolean = false,
    val busy: Boolean = false,
    val error: String? = null,
    val notice: String? = null,
    val threads: List<ThreadSummary> = emptyList(),
    val models: List<ModelInfo> = emptyList(),
    val projects: List<ProjectInfo> = emptyList(),
    val selectedModel: String = "",
    val selectedEffort: String = "",
    val selectedCwd: String = "",
    val activeThread: ThreadSummary? = null,
    val messages: List<MessageItem> = emptyList(),
    val streaming: Map<String, String> = emptyMap(),
    val approvals: List<ApprovalRequest> = emptyList(),
    val task: TaskSnapshot = TaskSnapshot(),
    val account: Account = Account(),
    val working: Boolean = false,
    val search: String = "",
    val darkTheme: Boolean = true,
    val messageCount: Int = 0,
    val truncated: Boolean = false,
    val restoreInput: String = "",
    val discovered: List<DiscoveredBridge> = emptyList(),
    val discovering: Boolean = false,
    val discoveryPort: Int = ApiClient.DEFAULT_DISCOVERY_PORT,
) {
    val modelLabel: String
        get() = models.firstOrNull { it.slug == selectedModel || it.id == selectedModel }?.label
            ?: selectedModel.ifBlank { "默认模型" }
}

class AppViewModel(application: Application) : AndroidViewModel(application) {
    private val appContext: Application = application
    private val settings = SettingsStore(application)
    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var api: ApiClient? = null
    private var streamJob: Job? = null
    private var pollJob: Job? = null
    private var listJob: Job? = null
    private var reconcileJob: Job? = null
    private var reconcilePending = false

    init {
        val base = settings.baseUrl
        val paired = settings.isPaired
        _state.update {
            it.copy(
                baseUrl = base,
                bridgeName = settings.bridgeName,
                bridgeVersion = settings.bridgeVersion,
                phoneId = settings.phoneId,
                paired = paired,
                screen = if (paired) AppScreen.Threads else AppScreen.Pair,
                selectedModel = settings.selectedModel,
                selectedEffort = settings.selectedEffort,
                selectedCwd = settings.selectedCwd,
                darkTheme = settings.darkTheme,
                discoveryPort = settings.discoveryPort,
            )
        }
        if (paired) {
            api = ApiClient(base, settings.token)
            refresh()
        }
    }

    private fun visibleThreadStatus(
        threadId: String,
        status: String,
        completedAt: Long,
        markSeen: Boolean,
    ): String {
        if (markSeen && status == "completed" && completedAt > 0) {
            settings.markCompletionSeen(threadId, completedAt)
        }
        return resolveThreadStatus(status, completedAt, settings.completionSeenAt(threadId))
    }

    private fun applyThreadSnapshot(
        state: UiState,
        thread: ThreadSummary,
        task: TaskSnapshot? = null,
    ): UiState {
        val threadId = thread.thread_id.ifBlank { task?.thread_id.orEmpty() }
        if (threadId.isBlank()) return state
        val completedAt = maxOf(thread.completed_at, task?.completed_at_epoch ?: 0L)
        val isActive = state.activeThread?.thread_id == threadId
        val visibleStatus = visibleThreadStatus(
            threadId,
            thread.status,
            completedAt,
            markSeen = isActive && state.screen == AppScreen.Chat,
        )
        val visibleThread = thread.copy(status = visibleStatus, completed_at = completedAt)
        val visibleTask = when {
            task == null -> state.task
            task.thread_id.isBlank() -> task.copy(
                thread_id = threadId,
                status = visibleStatus,
                title = task.title.ifBlank { thread.title },
                completed_at_epoch = task.completed_at_epoch.coerceAtLeast(completedAt),
            )
            task.thread_id == threadId -> task.copy(
                status = visibleStatus,
                title = task.title.ifBlank { thread.title },
                completed_at_epoch = task.completed_at_epoch.coerceAtLeast(completedAt),
            )
            else -> state.task
        }
        return state.copy(
            threads = state.threads.map { row ->
                if (row.thread_id == threadId) visibleThread else row
            }.let { rows ->
                if (rows.any { it.thread_id == threadId }) rows else listOf(visibleThread) + rows
            },
            activeThread = if (isActive) visibleThread else state.activeThread,
            task = if (isActive) visibleTask else state.task,
            working = if (isActive) visibleTask.isWorking else state.working,
        )
    }

    // -------------------------------------------------------------- pairing
    fun discoverBridges() {
        if (_state.value.discovering) return
        _state.update { it.copy(discovering = true, error = null) }
        viewModelScope.launch {
            val found = Discovery.discover(_state.value.discoveryPort)
            _state.update {
                it.copy(
                    discovered = found,
                    discovering = false,
                    notice = if (found.isEmpty()) "没有发现电脑，请确认手机和电脑在同一 Wi-Fi" else null,
                )
            }
        }
    }

    fun pair(baseUrl: String, code: String) {
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, error = null, notice = null) }
        viewModelScope.launch {
            try {
                val client = ApiClient(baseUrl)
                val ping = client.ping()
                val response = withContext(Dispatchers.IO) { client.pair(code.trim(), phoneName()) }
                if (!response.ok || response.token.isBlank()) {
                    throw ApiException("配对失败，请重新生成配对码")
                }
                settings.baseUrl = client.base
                settings.token = response.token
                settings.phoneId = response.phone_id
                settings.bridgeName = response.bridge.name.ifBlank { ping.name }
                settings.bridgeVersion = response.bridge.version.ifBlank { ping.version }
                api = ApiClient(client.base, response.token)
                _state.update {
                    it.copy(
                        busy = false,
                        paired = true,
                        baseUrl = client.base,
                        phoneId = response.phone_id,
                        bridgeName = response.bridge.name.ifBlank { ping.name },
                        bridgeVersion = response.bridge.version.ifBlank { ping.version },
                        connected = true,
                        screen = AppScreen.Threads,
                        notice = "已连接 ${response.bridge.name.ifBlank { ping.name }}",
                    )
                }
                CompletionNotificationService.start(appContext)
                refresh()
            } catch (error: Exception) {
                _state.update { it.copy(busy = false, error = friendly(error)) }
            }
        }
    }

    private fun phoneName(): String {
        val manufacturer = android.os.Build.MANUFACTURER.orEmpty()
        val model = android.os.Build.MODEL.orEmpty()
        return listOf(manufacturer, model).filter { it.isNotBlank() }.joinToString(" ").ifBlank { "Android" }
    }

    fun unpair() {
        listJob?.cancel()
        listJob = null
        reconcileJob?.cancel()
        reconcileJob = null
        CompletionNotificationService.stop(appContext)
        settings.clearPairing()
        closeThread()
        api = null
        _state.update {
            it.copy(
                paired = false,
                connected = false,
                screen = AppScreen.Pair,
                threads = emptyList(),
                messages = emptyList(),
                approvals = emptyList(),
                activeThread = null,
            )
        }
    }

    // -------------------------------------------------------------- refresh
    fun refresh(full: Boolean = true) {
        val client = api ?: return
        if (listJob?.isActive != true) {
            listJob = viewModelScope.launch {
                while (true) {
                    delay(3000)
                    runCatching { client.threads(_state.value.search) }.onSuccess { response ->
                        if (api !== client) return@onSuccess
                        _state.update { state ->
                            val incoming = response.threads.map { thread ->
                                val seen = settings.completionSeenAt(thread.thread_id)
                                thread.withCompletionSeen(seen)
                            }
                            state.copy(
                                threads = mergeThreadSnapshots(
                                    state.threads,
                                    incoming,
                                    state.activeThread?.thread_id,
                                ),
                                activeThread = state.activeThread?.let { current ->
                                    incoming.firstOrNull { it.thread_id == current.thread_id } ?: current
                                },
                            )
                        }
                    }
                }
            }
        }
        viewModelScope.launch {
            try {
                if (full) client.negotiateCapabilities()
                val status = client.status()
                _state.update {
                    it.copy(
                        connected = true,
                        account = status.account,
                        task = if (it.activeThread == null) status.task else it.task,
                        approvals = status.pending_approvals,
                        working = if (it.activeThread == null) status.task.isWorking else it.working,
                    )
                }
            } catch (error: Exception) {
                handleError(error)
            }
            try {
                val threads = client.threads(_state.value.search)
                _state.update { state ->
                    var next = state.copy(connected = true)
                    for (thread in threads.threads) {
                        next = applyThreadSnapshot(next, thread)
                    }
                    next
                }
            } catch (error: Exception) {
                handleError(error)
            }
            if (full) {
                loadProjects()
                loadModels()
            }
        }
    }

    private fun reconcileEvents() {
        reconcilePending = true
        if (reconcileJob?.isActive == true) return
        val client = api ?: return
        reconcileJob = viewModelScope.launch {
            while (reconcilePending && api === client) {
                delay(500)
                reconcilePending = false
                runCatching { client.threads(_state.value.search) }.onSuccess { response ->
                    if (api === client) _state.update { state ->
                        var next = state.copy(connected = true)
                        for (thread in response.threads) next = applyThreadSnapshot(next, thread)
                        next
                    }
                }
                val threadId = _state.value.activeThread?.thread_id ?: continue
                runCatching { client.thread(threadId) }.onSuccess { detail ->
                    if (api === client && _state.value.activeThread?.thread_id == threadId) {
                        _state.update { state ->
                            applyThreadSnapshot(state, detail.thread, detail.task).copy(
                                messages = detail.messages,
                                approvals = detail.pending_approvals,
                                messageCount = detail.message_count,
                            )
                        }
                    }
                }
            }
        }
    }

    private fun loadModels() {
        val client = api ?: return
        viewModelScope.launch {
            runCatching { client.models() }
                .onSuccess { response ->
                    val selected = _state.value.selectedModel
                    val chosen = response.models.firstOrNull { it.slug == selected || it.id == selected }
                        ?: response.models.firstOrNull { it.is_default }
                        ?: response.models.firstOrNull()
                    _state.update {
                        it.copy(
                            models = response.models,
                            selectedModel = chosen?.slug ?: selected,
                            selectedEffort = it.selectedEffort.ifBlank { chosen?.default_effort ?: "" },
                        )
                    }
                    chosen?.slug?.let { slug -> settings.selectedModel = slug }
                }
        }
    }

    private fun loadProjects() {
        val client = api ?: return
        viewModelScope.launch {
            runCatching { client.projects() }
                .onSuccess { response ->
                    val cwd = _state.value.selectedCwd.ifBlank { response.default_cwd }
                    _state.update { it.copy(projects = response.projects, selectedCwd = cwd) }
                    settings.selectedCwd = cwd
                }
        }
    }

    fun setSearch(value: String) {
        _state.update { it.copy(search = value) }
    }

    fun submitSearch() {
        val client = api ?: return
        viewModelScope.launch {
            runCatching { client.threads(_state.value.search) }
                .onSuccess { response -> _state.update { it.copy(threads = response.threads) } }
                .onFailure { handleError(it) }
        }
    }
    // ------------------------------------------------------------- threads
    fun openThread(threadId: String) {
        val client = api ?: return
        val summary = _state.value.threads.firstOrNull { it.thread_id == threadId }
        if (summary?.status == "completed" && summary.completed_at > 0) {
            settings.markCompletionSeen(threadId, summary.completed_at)
        }
        val seenAt = settings.completionSeenAt(threadId)
        val visibleSummary = summary?.withCompletionSeen(seenAt) ?: ThreadSummary(thread_id = threadId)
        _state.update { state ->
            state.copy(
                screen = AppScreen.Chat,
                activeThread = visibleSummary,
                threads = state.threads.map { thread ->
                    if (thread.thread_id == threadId) thread.withCompletionSeen(seenAt) else thread
                },
                messages = emptyList(),
                streaming = emptyMap(),
                approvals = emptyList(),
                busy = true,
                working = false,
            )
        }
        viewModelScope.launch {
            try {
                val detail = client.thread(threadId)
                _state.update { state ->
                    applyThreadSnapshot(state, detail.thread, detail.task).copy(
                        messages = detail.messages,
                        approvals = detail.pending_approvals,
                        messageCount = detail.message_count,
                        truncated = detail.truncated,
                        busy = false,
                    )
                }
                observeThread(threadId)
            } catch (error: Exception) {
                _state.update { it.copy(busy = false) }
                handleError(error)
            }
        }
    }

    fun closeThread() {
        streamJob?.cancel()
        streamJob = null
        pollJob?.cancel()
        pollJob = null
        _state.update {
            it.copy(screen = AppScreen.Threads, activeThread = null, messages = emptyList(), streaming = emptyMap())
        }
    }

    private fun observeThread(threadId: String) {
        val client = api ?: return
        streamJob?.cancel()
        streamJob = viewModelScope.launch {
            client.events(threadId).collect { event ->
                if (event.type == "stream_error") {
                    _state.update { it.copy(connected = false) }
                } else {
                    applyEvent(event)
                }
            }
        }
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            while (true) {
                delay(4000)
                runCatching { client.thread(threadId) }.onSuccess { detail ->
                    if (_state.value.activeThread?.thread_id != threadId) return@onSuccess
                    _state.update { state ->
                        applyThreadSnapshot(state, detail.thread, detail.task).copy(
                            messages = detail.messages,
                            approvals = detail.pending_approvals,
                            messageCount = detail.message_count,
                        )
                    }
                }
            }
        }
    }

    fun send(text: String) {
        val client = api ?: return
        val threadId = _state.value.activeThread?.thread_id ?: return
        val content = text.trim()
        if (content.isEmpty()) return
        val local = MessageItem(
            id = "local-" + UUID.randomUUID().toString(),
            kind = "user",
            text = content,
            created_at = System.currentTimeMillis() / 1000,
        )
        _state.update {
            it.copy(
                messages = it.messages + local,
                working = true,
                error = null,
            )
        }
        viewModelScope.launch {
            try {
                client.sendMessage(
                    threadId = threadId,
                    text = content,
                    model = _state.value.selectedModel,
                    effort = _state.value.selectedEffort,
                    steer = false,
                )
            } catch (error: Exception) {
                _state.update {
                    it.copy(
                        messages = it.messages.filterNot { item -> item.id == local.id },
                        working = false,
                        restoreInput = content,
                    )
                }
                handleError(error)
            }
        }
    }

    fun stop() {
        val client = api ?: return
        val threadId = _state.value.activeThread?.thread_id ?: return
        viewModelScope.launch {
            runCatching { client.interrupt(threadId, _state.value.task.turn_id) }
                .onSuccess { _state.update { it.copy(working = false, notice = "已请求中断") } }
                .onFailure { handleError(it) }
        }
    }

    fun newThread(cwd: String, model: String) {
        val client = api ?: return
        if (_state.value.busy) return
        // The create-thread dialog also owns the project picker, so keep its (cwd, model)
        // order here. The previous (model, cwd) order silently sent the project path as
        // the model name, which made the desktop reject the new task.
        val projectCwd = cwd.trim()
        val safeModel = model.takeIf { it.isNotBlank() && !it.contains("\\") && !Regex("^[A-Za-z]:[\\/]").containsMatchIn(it) }.orEmpty()
        _state.update { it.copy(busy = true, error = null) }
        viewModelScope.launch {
            try {
                val created = client.createThread(projectCwd, safeModel)
                val thread = created.thread
                settings.selectedCwd = projectCwd
                if (safeModel.isNotBlank()) {
                    settings.selectedModel = safeModel
                }
                _state.update {
                    it.copy(
                        busy = false,
                        selectedModel = safeModel.ifBlank { it.selectedModel },
                        selectedCwd = projectCwd,
                        threads = listOf(thread) + it.threads.filterNot { row -> row.thread_id == thread.thread_id },
                    )
                }
                openThread(thread.thread_id)
            } catch (error: Exception) {
                _state.update { it.copy(busy = false) }
                handleError(error)
            }
        }
    }

    fun resolveApproval(requestId: Int, decision: String) {
        val client = api ?: return
        viewModelScope.launch {
            runCatching { client.resolveApproval(requestId, decision) }
                .onSuccess {
                    _state.update { state ->
                        state.copy(approvals = state.approvals.filterNot { it.request_id == requestId })
                    }
                }
                .onFailure { handleError(it) }
        }
    }

    fun respondApproval(requestId: Int, response: kotlinx.serialization.json.JsonObject) {
        val client = api ?: return
        viewModelScope.launch {
            runCatching { client.respondApproval(requestId, response) }
                .onSuccess { result ->
                    if (result.resolved) _state.update { state ->
                        state.copy(approvals = state.approvals.filterNot { it.request_id == requestId })
                    }
                }.onFailure { handleError(it) }
        }
    }

    fun selectModel(model: String, effort: String = "") {
        settings.selectedModel = model
        settings.selectedEffort = effort
        _state.update { it.copy(selectedModel = model, selectedEffort = effort) }
    }

    fun selectEffort(effort: String) {
        settings.selectedEffort = effort
        _state.update { it.copy(selectedEffort = effort) }
    }

    fun selectProject(cwd: String) {
        settings.selectedCwd = cwd
        _state.update { it.copy(selectedCwd = cwd) }
    }

    fun openSettings() {
        _state.update { it.copy(screen = AppScreen.Settings) }
    }

    fun closeSettings() {
        _state.update { it.copy(screen = if (it.activeThread != null) AppScreen.Chat else AppScreen.Threads) }
    }

    fun setDarkTheme(enabled: Boolean) {
        settings.darkTheme = enabled
        _state.update { it.copy(darkTheme = enabled) }
    }

    fun setDiscoveryPort(port: Int) {
        if (port !in 1..65535) return
        settings.discoveryPort = port
        _state.update { it.copy(discoveryPort = port) }
    }

    fun dismissError() {
        _state.update { it.copy(error = null) }
    }

    fun dismissNotice() {
        _state.update { it.copy(notice = null) }
    }

    fun consumeRestoreInput() {
        _state.update { it.copy(restoreInput = "") }
    }

    // -------------------------------------------------------------- events
    private fun applyEvent(event: StreamEvent) {
        when (event.type) {
            "agent_delta" -> {
                if (event.item_id.isBlank()) return
                _state.update { state ->
                    val current = state.streaming[event.item_id].orEmpty()
                    state.copy(streaming = state.streaming + (event.item_id to current + event.delta))
                }
            }
            "item_started", "item_completed" -> {
                val item = event.item ?: return
                val resolved = if (item.kind == "agent" && item.text.isBlank()) {
                    item.copy(text = _state.value.streaming[item.id].orEmpty())
                } else {
                    item
                }
                _state.update { state ->
                    val base = if (resolved.kind == "user") {
                        state.messages.filterNot { it.id.startsWith("local-") && it.text == resolved.text }
                    } else {
                        state.messages
                    }
                    state.copy(
                        messages = upsert(base, resolved),
                        streaming = state.streaming - item.id,
                        messageCount = state.messageCount + (if (base.any { it.id == resolved.id }) 0 else 1),
                    )
                }
            }
            "turn_started" -> {
                val threadId = event.thread_id.ifBlank { _state.value.activeThread?.thread_id.orEmpty() }
                if (threadId.isBlank()) {
                    _state.update { it.copy(working = true) }
                } else {
                    _state.update { state ->
                        val row = state.activeThread?.takeIf { it.thread_id == threadId }
                            ?: state.threads.firstOrNull { it.thread_id == threadId }
                            ?: ThreadSummary(thread_id = threadId)
                        val workingRow = row.copy(status = "working")
                        state.copy(
                            working = workingAfterTurnEvent(
                                current = state.working,
                                activeThreadId = state.activeThread?.thread_id,
                                eventThreadId = threadId,
                                running = true,
                            ),
                            threads = state.threads.map { item ->
                                if (item.thread_id == threadId) workingRow else item
                            }.let { rows -> if (rows.any { it.thread_id == threadId }) rows else listOf(workingRow) + rows },
                            activeThread = if (state.activeThread?.thread_id == threadId) workingRow else state.activeThread,
                            task = if (state.activeThread?.thread_id == threadId) state.task.copy(
                                thread_id = threadId,
                                turn_id = event.turn_id.ifBlank { state.task.turn_id },
                                status = "working",
                                title = state.task.title.ifBlank { workingRow.title },
                            ) else state.task,
                        )
                    }
                }
            }
            "threads_changed", "hello", "resync_required" -> {
                reconcileEvents()
            }
            "turn_completed" -> {
                val threadId = event.thread_id.ifBlank { _state.value.activeThread?.thread_id.orEmpty() }
                val completionStatus = if (event.status == "failed") "failed" else "completed"
                val completedAt = System.currentTimeMillis() / 1000
                val viewing = _state.value.screen == AppScreen.Chat && _state.value.activeThread?.thread_id == threadId
                if (completionStatus == "completed" && viewing && threadId.isNotBlank()) {
                    settings.markCompletionSeen(threadId, completedAt)
                }
                val visibleStatus = if (completionStatus == "completed" && viewing) "idle" else completionStatus
                _state.update { state ->
                    state.copy(
                        working = workingAfterTurnEvent(
                            current = state.working,
                            activeThreadId = state.activeThread?.thread_id,
                            eventThreadId = threadId,
                            running = false,
                        ),
                        threads = state.threads.map { thread ->
                            if (thread.thread_id == threadId) {
                                thread.copy(status = visibleStatus, completed_at = completedAt)
                            } else {
                                thread
                            }
                        },
                        activeThread = state.activeThread?.let { thread ->
                            if (thread.thread_id == threadId) {
                                thread.copy(status = visibleStatus, completed_at = completedAt)
                            } else {
                                thread
                            }
                        },
                        task = if (state.activeThread?.thread_id == threadId) {
                            state.task.copy(status = visibleStatus)
                        } else {
                            state.task
                        },
                    )
                }
                if (!threadId.isNullOrBlank()) {
                    api?.let { client ->
                        viewModelScope.launch {
                            runCatching { client.thread(threadId) }.onSuccess { detail ->
                                if (_state.value.activeThread?.thread_id != threadId) return@onSuccess
                                _state.update { state ->
                                    applyThreadSnapshot(state, detail.thread, detail.task).copy(
                                        messages = detail.messages,
                                        approvals = detail.pending_approvals,
                                        messageCount = detail.message_count,
                                    )
                                }
                            }
                            refresh(full = false)
                        }
                    }
                }
            }
            "turn_interrupted" -> {
                val threadId = event.thread_id.ifBlank {
                    _state.value.activeThread?.thread_id.orEmpty()
                }
                _state.update { state ->
                    state.copy(
                        working = workingAfterTurnEvent(
                            current = state.working,
                            activeThreadId = state.activeThread?.thread_id,
                            eventThreadId = threadId,
                            running = false,
                        ),
                    )
                }
            }
            "turn_steered" -> Unit
            "thread_status" -> {
                val threadId = event.thread_id.ifBlank { _state.value.activeThread?.thread_id.orEmpty() }
                val flags = event.active_flags
                val status = when {
                    event.status.lowercase() in setOf("active", "inprogress", "in_progress", "running") || flags.isNotEmpty() -> "working"
                    event.status.lowercase() in setOf("idle", "notloaded", "not_loaded") -> "idle"
                    event.status.lowercase() == "completed" -> "completed"
                    event.status.lowercase() in setOf("failed", "interrupted", "cancelled", "canceled") -> "failed"
                    else -> ""
                }
                if (threadId.isBlank() || status.isBlank()) return@applyEvent
                _state.update { state ->
                    val row = state.activeThread?.takeIf { it.thread_id == threadId }
                        ?: state.threads.firstOrNull { it.thread_id == threadId }
                        ?: ThreadSummary(thread_id = threadId)
                    val completedAt = if (status == "completed") {
                        maxOf(System.currentTimeMillis() / 1000, row.completed_at)
                    } else {
                        row.completed_at
                    }
                    val seenAt = settings.completionSeenAt(threadId)
                    val visibleStatus = if (status == "completed" && state.screen == AppScreen.Chat &&
                        state.activeThread?.thread_id == threadId) {
                        settings.markCompletionSeen(threadId, completedAt)
                        "idle"
                    } else {
                        resolveThreadStatus(status, completedAt, seenAt)
                    }
                    val updated = row.copy(status = visibleStatus, completed_at = completedAt)
                    state.copy(
                        working = if (state.activeThread?.thread_id == threadId) visibleStatus == "working" || visibleStatus == "waiting_user" else state.working,
                        threads = state.threads.map { item ->
                            if (item.thread_id == threadId) updated else item
                        }.let { rows -> if (rows.any { it.thread_id == threadId }) rows else listOf(updated) + rows },
                        activeThread = if (state.activeThread?.thread_id == threadId) updated else state.activeThread,
                        task = if (state.activeThread?.thread_id == threadId) state.task.copy(
                            thread_id = threadId,
                            status = visibleStatus,
                            completed_at_epoch = if (completedAt > 0) completedAt else state.task.completed_at_epoch,
                        ) else state.task,
                    )
                }
            }
            "token_usage" -> _state.update {
                it.copy(task = it.task.copy(tokenCount = event.total_tokens, tokenCountAvailable = event.total_tokens > 0))
            }
            "thread_name" -> {
                val title = event.title
                val threadId = event.thread_id.ifBlank { _state.value.activeThread?.thread_id.orEmpty() }
                if (title.isNotBlank() && threadId.isNotBlank()) {
                    _state.update { state ->
                        state.copy(
                            threads = state.threads.withThreadTitle(threadId, title),
                            activeThread = state.activeThread?.let { thread ->
                                if (thread.thread_id == threadId) thread.copy(title = title) else thread
                            },
                            task = state.task.withThreadTitle(threadId, title),
                        )
                    }
                }
            }
            "approval_request" -> {
                val approval = ApprovalRequest(
                    request_id = event.request_id,
                    kind = event.kind,
                    thread_id = event.thread_id,
                    turn_id = event.turn_id,
                    item_id = event.item_id,
                    command = event.command,
                    cwd = event.cwd,
                    reason = event.reason,
                    created_at = event.created_at,
                    expires_at = event.expires_at,
                    raw = event.raw,
                )
                _state.update { state ->
                    state.copy(
                        working = true,
                        approvals = state.approvals.filterNot { it.request_id == approval.request_id } + approval,
                    )
                }
            }
            "approval_resolved" -> _state.update { state ->
                state.copy(approvals = state.approvals.filterNot { it.request_id == event.request_id })
            }
            "error" -> if (event.message.isNotBlank()) {
                _state.update { it.copy(error = event.message) }
            }
        }
    }

    private fun upsert(messages: List<MessageItem>, item: MessageItem): List<MessageItem> {
        val index = messages.indexOfFirst { it.id == item.id }
        if (index < 0) return messages + item
        val updated = messages.toMutableList()
        updated[index] = item
        return updated
    }

    private fun mergeApprovals(
        current: List<ApprovalRequest>,
        incoming: List<ApprovalRequest>,
    ): List<ApprovalRequest> {
        val merged = current.associateBy { it.request_id }.toMutableMap()
        incoming.forEach { merged[it.request_id] = it }
        return merged.values.sortedBy { it.request_id }
    }

    private fun handleError(error: Throwable) {
        if (error is ApiException && error.unauthorized) {
            unpair()
        }
        _state.update { it.copy(error = friendly(error), connected = !(error is ApiException && error.unauthorized)) }
    }

    private fun friendly(error: Throwable): String {
        val message = error.message.orEmpty()
        return when {
            message.contains("Invalid IPC frame", ignoreCase = true) ||
                message.contains("IPC", ignoreCase = true) -> "电脑端连接正在恢复，请稍后重试"
            error is ApiException && message.isNotBlank() -> message
            message.isNotBlank() -> message
            else -> "网络不可用，请检查手机与电脑是否在同一 Wi-Fi"
        }
    }
}
