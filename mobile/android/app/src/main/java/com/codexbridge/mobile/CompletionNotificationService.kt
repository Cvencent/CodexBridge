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
        startAsForeground()
        if (job?.isActive != true) {
            job = scope.launch { listen(settings) }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        job?.cancel()
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
        while (job?.isActive != false) {
            try {
                val client = ApiClient(settings.baseUrl, settings.token)
                client.events().collect { event ->
                    if (event.type != "turn_completed" || event.status == "failed") return@collect
                    if (event.thread_id.isBlank() || event.turn_id.isBlank()) return@collect
                    if (!settings.markNotificationSent(event.thread_id, event.turn_id)) return@collect
                    notifyCompletion(event.thread_id, event.turn_id)
                }
                delay(RETRY_MS)
            } catch (_: Exception) {
                delay(RETRY_MS)
            }
        }
    }

    private fun notifyCompletion(threadId: String, turnId: String) {
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
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()
        NotificationManagerCompat.from(this).notify(completionId(threadId, turnId), notification)
    }

    private fun createChannels() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(LISTEN_CHANNEL, "Codex 后台连接", NotificationManager.IMPORTANCE_LOW).apply {
                description = "保持 Codex 完成后台通知连接"
            },
        )
        manager.createNotificationChannel(
            NotificationChannel(COMPLETION_CHANNEL, "任务完成", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Codex 任务完成提醒"
            },
        )
    }

    companion object {
        private const val LISTEN_CHANNEL = "codexbridge_listen"
        private const val COMPLETION_CHANNEL = "codexbridge_completion"
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

        private fun completionId(threadId: String, turnId: String): Int =
            5000 + (threadId + turnId).hashCode() % 100000
    }
}
