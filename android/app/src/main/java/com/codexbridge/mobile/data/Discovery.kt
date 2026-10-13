package com.codexbridge.mobile.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

@Serializable
data class DiscoveryPayload(
    val service: String = "",
    val name: String = "",
    val version: String = "",
    val port: Int = 8790,
    val discovery_port: Int = 8766,
    val requires_pairing: Boolean = true,
)

data class DiscoveredBridge(
    val host: String,
    val port: Int,
    val name: String,
    val version: String,
) {
    val baseUrl: String get() = "http://$host:$port"
}

object Discovery {
    private const val DISCOVERY_PORT = 8766
    private const val TIMEOUT_MS = 2600L
    private val requestBytes = "CODEXBRIDGE_COMPANION_DISCOVER".toByteArray(Charsets.UTF_8)
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    suspend fun discover(timeoutMs: Long = TIMEOUT_MS): List<DiscoveredBridge> =
        withContext(Dispatchers.IO) {
            val socket = DatagramSocket()
            try {
                socket.broadcast = true
                socket.soTimeout = 500
                socket.send(
                    DatagramPacket(
                        requestBytes,
                        requestBytes.size,
                        InetAddress.getByName("255.255.255.255"),
                        DISCOVERY_PORT,
                    ),
                )
                val found = linkedMapOf<String, DiscoveredBridge>()
                val deadline = System.currentTimeMillis() + timeoutMs
                val buffer = ByteArray(1024)
                while (System.currentTimeMillis() < deadline) {
                    val response = DatagramPacket(buffer, buffer.size)
                    try {
                        socket.receive(response)
                    } catch (_error: SocketTimeoutException) {
                        continue
                    }
                    val text = String(response.data, 0, response.length, Charsets.UTF_8)
                    val payload = runCatching {
                        json.decodeFromString(DiscoveryPayload.serializer(), text)
                    }.getOrNull() ?: continue
                    if (payload.service != "codexbridge-companion") continue
                    val host = response.address?.hostAddress ?: continue
                    found[host] = DiscoveredBridge(
                        host = host,
                        port = payload.port,
                        name = payload.name,
                        version = payload.version,
                    )
                }
                found.values.toList()
            } catch (_error: Exception) {
                emptyList()
            } finally {
                socket.close()
            }
        }
}