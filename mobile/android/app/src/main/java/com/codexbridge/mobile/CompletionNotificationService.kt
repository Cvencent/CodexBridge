package com.codexbridge.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.codexbridge.mobile.data.ApiClient
import com.codexbridge.mobile.data.SettingsStore
import com.codexbridge.mobile.data.shouldReconcileCompletion
import com.codexbridge.mobile.data.notificationResumeSince
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class CompletionNotificationService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    private var restoreNotificationJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        createChannels()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val settings = SettingsStore(this)
        if (!settings.isPaired) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_RESTORE_LISTENING) {
            restoreNotificationJob?.cancel()
            startAsForeground()
            return START_STICKY
        }
        startAsForeground()
        if (job?.isActive != true) {
            job = scope.launch { listen(settings) }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        job?.cancel()
        restoreNotificationJob?.cancel()
        scope.cancel()
        super.onDestroy()
    }

    private fun startAsForeground() {
        val launch = PendingIntent.getActivity(
            this,
            100,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, LISTEN_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("CodexBridge")
            .setContentText("正在监听 Codex 任务完成")
            .setContentIntent(launch)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
        postForegroundNotification(notification)
    }

    private fun postForegroundNotification(notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ServiceCompat.startForeground(
                this,
                LISTEN_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(LISTEN_NOTIFICATION_ID, notification)
        }
    }

    private suspend fun listen(settings: SettingsStore) {
        val listeningSince = notificationResumeSince(settings.notificationWatermark, System.currentTimeMillis() / 1000)
        if (settings.notificationWatermark == 0L) settings.notificationWatermark = listeningSince
        while (job?.isActive != false) {
            try {
                val client = ApiClient(settings.baseUrl, settings.token)
                client.events().collect { event ->
                    if (event.type == "hello" || event.type == "resync_required") {
                        val syncStarted = System.currentTimeMillis() / 1000
                        var complete = true
                        runCatching { client.threads() }.onSuccess { response ->
                            for (thread in response.threads) {
                                if (!shouldReconcileCompletion(thread.status, thread.completed_at, listeningSince)) continue
                                runCatching { client.thread(thread.thread_id) }.onSuccess { detail ->
                                    val key = detail.task.turn_id.ifBlank {
                                        detail.thread.completed_at.takeIf { it > 0 }?.toString().orEmpty()
                                    }
                                    deliverCompletion(settings, thread.thread_id, key)
                                }.onFailure { complete = false }
                            }
                        }.onFailure { complete = false }
                        if (complete) settings.notificationWatermark = syncStarted
                    }
                    if (event.type != "turn_completed" || event.status == "failed") return@collect
                    val key = event.turn_id.ifBlank {
                        runCatching { client.thread(event.thread_id) }
                            .getOrNull()
                            ?.thread
                            ?.completed_at
                            ?.takeIf { it > 0 }
                            ?.toString()
                            .orEmpty()
                    }
                    deliverCompletion(settings, event.thread_id, key)
                }
                delay(RETRY_MS)
            } catch (_: Exception) {
                delay(RETRY_MS)
            }
        }
    }

    private fun deliverCompletion(settings: SettingsStore, threadId: String, turnId: String) {
        if (threadId.isBlank() || turnId.isBlank()) return
        if (!NotificationManagerCompat.from(this).areNotificationsEnabled()) return
        if (!settings.markNotificationSent(threadId, turnId)) return
        notifyCompletion(threadId)
    }

    private fun notifyCompletion(threadId: String) {
        val intent = Intent(this, MainActivity::class.java)
            .putExtra(EXTRA_THREAD_ID, threadId)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pending = PendingIntent.getActivity(
            this,
            threadId.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(this, COMPLETION_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("Codex 任务已完成")
            .setContentText("点开查看结果")
            .setContentIntent(pending)
            .setOngoing(true)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setDefaults(Notification.DEFAULT_SOUND or Notification.DEFAULT_VIBRATE)
            .build()
        postForegroundNotification(notification)
        restoreNotificationJob?.cancel()
        restoreNotificationJob = scope.launch {
            delay(COMPLETION_NOTIFICATION_MS)
            startAsForeground()
        }
    }

    private fun createChannels() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(LISTEN_CHANNEL, "Codex 后台连接", NotificationManager.IMPORTANCE_LOW).apply {
                description = "保持 Codex 完成后台通知连接"
            },
        )
        manager.createNotificationChannel(
            NotificationChannel(COMPLETION_CHANNEL, "任务完成", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Codex 任务完成提醒"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 250, 120, 250)
                setSound(android.provider.Settings.System.DEFAULT_NOTIFICATION_URI,
                    android.media.AudioAttributes.Builder()
                        .setUsage(android.media.AudioAttributes.USAGE_NOTIFICATION).build())
            },
        )
    }

    companion object {
        private const val LISTEN_CHANNEL = "codexbridge_listen"
        private const val COMPLETION_CHANNEL = "codexbridge_completion_alerts"
        private const val ACTION_RESTORE_LISTENING = "com.codexbridge.mobile.action.RESTORE_LISTENING"
        private const val COMPLETION_NOTIFICATION_MS = 60_000L
        private const val LISTEN_NOTIFICATION_ID = 4101
        private const val RETRY_MS = 3000L
        const val EXTRA_THREAD_ID = "thread_id"

        fun start(context: Context) {
            val intent = Intent(context, CompletionNotificationService::class.java)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, CompletionNotificationService::class.java))
        }

        fun restoreListening(context: Context) {
            val intent = Intent(context, CompletionNotificationService::class.java)
                .setAction(ACTION_RESTORE_LISTENING)
            ContextCompat.startForegroundService(context, intent)
        }
    }
}
