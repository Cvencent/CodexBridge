package com.codexbridge.mobile.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ApiClientTest {
    @Test
    fun addsSchemeAndDefaultPortForBareHost() {
        assertEquals("http://192.168.1.5:8791", ApiClient.normalizeBase("192.168.1.5"))
    }

    @Test
    fun keepsExplicitPort() {
        assertEquals("http://192.168.1.5:9000", ApiClient.normalizeBase("http://192.168.1.5:9000"))
    }

    @Test
    fun trimsTrailingSlash() {
        assertEquals("http://192.168.1.5:8791", ApiClient.normalizeBase("http://192.168.1.5:8791/"))
    }

    @Test
    fun keepsHttpsAndHostname() {
        assertEquals("https://bridge.local:8791", ApiClient.normalizeBase("https://bridge.local"))
        assertEquals("http://bridge.local:8791", ApiClient.normalizeBase("bridge.local"))
    }

    @Test
    fun migratesTheOldDefaultPort() {
        assertEquals("http://192.168.1.5:8791", ApiClient.normalizeBase("http://192.168.1.5:8790/"))
    }

    @Test
    fun storedAddressMigrationKeepsBlankValueUnpaired() {
        assertEquals("", ApiClient.migrateStoredBase(""))
        assertEquals("http://192.168.1.5:8791", ApiClient.migrateStoredBase("http://192.168.1.5:8790/"))
    }

    @Test
    fun rejectsBlankAddress() {
        assertThrows(ApiException::class.java) { ApiClient.normalizeBase("   ") }
    }
}
