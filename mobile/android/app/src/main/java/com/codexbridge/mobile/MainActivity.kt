package com.codexbridge.mobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.codexbridge.mobile.ui.ChatScreen
import com.codexbridge.mobile.ui.CodexBridgeTheme
import com.codexbridge.mobile.ui.PairScreen
import com.codexbridge.mobile.ui.SettingsScreen
import com.codexbridge.mobile.ui.ThreadsScreen

class MainActivity : ComponentActivity() {
    private val viewModel: AppViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestNotificationPermission()
        openThreadFromIntent(intent)
        if (viewModel.state.value.paired) CompletionNotificationService.start(this)
        enableEdgeToEdge()
        setContent {
            val state by viewModel.state.collectAsState()
            CodexBridgeTheme(darkTheme = state.darkTheme) {
                AppRoot(state = state, viewModel = viewModel)
            }
        }
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        openThreadFromIntent(intent)
    }

    private fun openThreadFromIntent(intent: android.content.Intent?) {
        val threadId = intent?.getStringExtra(CompletionNotificationService.EXTRA_THREAD_ID).orEmpty()
        if (threadId.isNotBlank()) viewModel.openThread(threadId)
    }

    private fun requestNotificationPermission() {
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(
                arrayOf(android.Manifest.permission.POST_NOTIFICATIONS),
                NOTIFICATION_PERMISSION_REQUEST,
            )
        }
    }

    companion object {
        private const val NOTIFICATION_PERMISSION_REQUEST = 4102
    }
}

@Composable
private fun AppRoot(state: UiState, viewModel: AppViewModel) {
    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(state.error, state.notice) {
        val message = state.error ?: state.notice
        if (message != null) {
            val isError = state.error != null
            snackbarHostState.showSnackbar(message)
            if (isError) viewModel.dismissError() else viewModel.dismissNotice()
        }
    }

    BackHandler(enabled = state.screen == AppScreen.Chat || state.screen == AppScreen.Settings) {
        when (state.screen) {
            AppScreen.Chat -> viewModel.closeThread()
            AppScreen.Settings -> viewModel.closeSettings()
            else -> Unit
        }
    }

    Box(modifier = Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        when (state.screen) {
            AppScreen.Pair -> PairScreen(
                state = state,
                onDiscover = viewModel::discoverBridges,
                onDiscoveryPort = viewModel::setDiscoveryPort,
                onPair = viewModel::pair,
            )
            AppScreen.Threads -> ThreadsScreen(
                state = state,
                onRefresh = viewModel::refresh,
                onOpenSettings = viewModel::openSettings,
                onOpenThread = viewModel::openThread,
                onSearchChange = viewModel::setSearch,
                onSearchSubmit = viewModel::submitSearch,
                onCreateThread = viewModel::newThread,
            )
            AppScreen.Chat -> ChatScreen(
                state = state,
                onBack = viewModel::closeThread,
                onSend = viewModel::send,
                onStop = viewModel::stop,
                onPickModel = { slug -> viewModel.selectModel(slug) },
                onResolveApproval = viewModel::resolveApproval,
            )
            AppScreen.Settings -> SettingsScreen(
                state = state,
                onBack = viewModel::closeSettings,
                onToggleTheme = viewModel::setDarkTheme,
                onDiscoveryPort = viewModel::setDiscoveryPort,
                onUnpair = viewModel::unpair,
            )
        }
        SnackbarHost(
            hostState = snackbarHostState,
            modifier = Modifier.align(Alignment.BottomCenter),
        )
    }
}
