package com.codexbridge.mobile

import android.app.Application
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
    val discovered: List<DiscoveredBridge> = emptyList(),
    val discovering: Boolean = false,
) {
    val modelLabel: String
        get() = models.firstOrNull { it.slug == selectedModel || it.id == selectedModel }?.label
            ?: selectedModel.ifBlank { "默认模型" }
}

class AppViewModel(application: Application) : AndroidViewModel(application) {
    private val settings = SettingsStore(application)
    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var api: ApiClient? = null
    private var streamJob: Job? = null
    private var pollJob: Job? = null

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
            )
        }
        if (paired) {
            api = ApiClient(base, settings.token)
            refresh()
        }
    }

    // -------------------------------------------------------------- pairing
    fun discoverBridges() {
        if (_state.value.discovering) return
        _state.update { it.copy(discovering = true, error = null) }
        viewModelScope.launch {
            val found = Discovery.discover()
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
    fun refresh() {
        val client = api ?: return
        viewModelScope.launch {
            try {
                val status = client.status()
                _state.update {
                    it.copy(
                        connected = true,
                        account = status.account,
                        task = status.task,
                        approvals = mergeApprovals(it.approvals, status.pending_approvals),
                        working = status.task.isWorking || it.working,
                    )
                }
            } catch (error: Exception) {
                handleError(error)
            }
            try {
                val threads = client.threads(_state.value.search)
                _state.update { it.copy(threads = threads.threads, connected = true) }
            } catch (error: Exception) {
                handleError(error)
            }
            loadModels()
            loadProjects()
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
        _state.update {
            it.copy(
                screen = AppScreen.Chat,
                activeThread = summary ?: ThreadSummary(thread_id = threadId),
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
                _state.update {
                    it.copy(
                        activeThread = detail.thread,
                        messages = detail.messages,
                        task = detail.task,
                        approvals = detail.pending_approvals,
                        working = detail.task.isWorking,
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
                if (!_state.value.working) continue
                runCatching { client.thread(threadId) }.onSuccess { detail ->
                    _state.update {
                        it.copy(
                            messages = detail.messages,
                            task = detail.task,
                            approvals = detail.pending_approvals,
                            working = detail.task.isWorking,
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
        val wasWorking = _state.value.working
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
                    steer = wasWorking,
                )
            } catch (error: Exception) {
                _state.update { it.copy(messages = it.messages.filterNot { item -> item.id == local.id }) }
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

    fun newThread(model: String, cwd: String) {
        val client = api ?: return
        if (_state.value.busy) return
        _state.update { it.copy(busy = true, error = null) }
        viewModelScope.launch {
            try {
                val created = client.createThread(cwd, model)
                val thread = created.thread
                settings.selectedCwd = cwd
                if (model.isNotBlank()) {
                    settings.selectedModel = model
                }
                _state.update {
                    it.copy(
                        busy = false,
                        selectedModel = model.ifBlank { it.selectedModel },
                        selectedCwd = cwd,
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

    fun dismissError() {
        _state.update { it.copy(error = null) }
    }

    fun dismissNotice() {
        _state.update { it.copy(notice = null) }
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
            "turn_started" -> _state.update { it.copy(working = true) }
            "turn_completed" -> {
                _state.update { it.copy(working = false) }
                val threadId = _state.value.activeThread?.thread_id
                if (!threadId.isNullOrBlank()) {
                    api?.let { client ->
                        viewModelScope.launch {
                            runCatching { client.thread(threadId) }.onSuccess { detail ->
                                _state.update {
                                    it.copy(
                                        messages = detail.messages,
                                        task = detail.task,
                                        approvals = detail.pending_approvals,
                                        working = detail.task.isWorking,
                                        messageCount = detail.message_count,
                                    )
                                }
                            }
                            refresh()
                        }
                    }
                }
            }
            "turn_interrupted" -> _state.update { it.copy(working = false) }
            "turn_steered" -> Unit
            "thread_status" -> {
                val flags = event.active_flags
                val active = event.status == "active" || flags.isNotEmpty()
                _state.update { it.copy(working = active || it.working && event.status != "idle") }
            }
            "token_usage" -> _state.update {
                it.copy(task = it.task.copy(tokenCount = event.total_tokens, tokenCountAvailable = event.total_tokens > 0))
            }
            "thread_name" -> {
                val title = event.title
                if (title.isNotBlank()) {
                    _state.update { it.copy(activeThread = it.activeThread?.copy(title = title)) }
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

    private fun friendly(error: Throwable): String = when (error) {
        is ApiException -> error.message ?: "请求失败"
        else -> error.message ?: "网络不可用，请检查手机与电脑是否在同一 Wi-Fi"
    }
}