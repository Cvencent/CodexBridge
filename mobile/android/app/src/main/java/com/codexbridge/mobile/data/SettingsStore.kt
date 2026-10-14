package com.codexbridge.mobile.data

import android.content.Context
import android.content.SharedPreferences

class SettingsStore(context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences("codexbridge_mobile", Context.MODE_PRIVATE)

    var baseUrl: String
        get() = prefs.getString("base_url", "") ?: ""
        set(value) = prefs.edit().putString("base_url", value).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value).apply()

    var phoneId: String
        get() = prefs.getString("phone_id", "") ?: ""
        set(value) = prefs.edit().putString("phone_id", value).apply()

    var bridgeName: String
        get() = prefs.getString("bridge_name", "") ?: ""
        set(value) = prefs.edit().putString("bridge_name", value).apply()

    var bridgeVersion: String
        get() = prefs.getString("bridge_version", "") ?: ""
        set(value) = prefs.edit().putString("bridge_version", value).apply()

    var selectedModel: String
        get() = prefs.getString("selected_model", "") ?: ""
        set(value) = prefs.edit().putString("selected_model", value).apply()

    var selectedEffort: String
        get() = prefs.getString("selected_effort", "") ?: ""
        set(value) = prefs.edit().putString("selected_effort", value).apply()

    var selectedCwd: String
        get() = prefs.getString("selected_cwd", "") ?: ""
        set(value) = prefs.edit().putString("selected_cwd", value).apply()

    var darkTheme: Boolean
        get() = prefs.getBoolean("dark_theme", true)
        set(value) = prefs.edit().putBoolean("dark_theme", value).apply()

    val isPaired: Boolean
        get() = baseUrl.isNotBlank() && token.isNotBlank()

    fun clearPairing() {
        prefs.edit().remove("token").remove("phone_id").apply()
    }

    fun clearAll() {
        prefs.edit().clear().apply()
    }
}