package com.codexbridge.mobile.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val DarkScheme = darkColorScheme(
    primary = Color(0xFF6EA8FF),
    onPrimary = Color(0xFF06121F),
    primaryContainer = Color(0xFF1B3A5F),
    onPrimaryContainer = Color(0xFFD7E7FF),
    secondary = Color(0xFF45C6A5),
    onSecondary = Color(0xFF04231B),
    secondaryContainer = Color(0xFF11463A),
    onSecondaryContainer = Color(0xFFC7F5E7),
    tertiary = Color(0xFFE7BE5C),
    onTertiary = Color(0xFF3A2A00),
    background = Color(0xFF0B0F14),
    onBackground = Color(0xFFE6EDF3),
    surface = Color(0xFF121821),
    onSurface = Color(0xFFE6EDF3),
    surfaceVariant = Color(0xFF1B2431),
    onSurfaceVariant = Color(0xFFA9B6C4),
    outline = Color(0xFF3A4553),
    error = Color(0xFFFF7B72),
    onError = Color(0xFF2A0A08),
)

private val LightScheme = lightColorScheme(
    primary = Color(0xFF1F6FEB),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFD7E7FF),
    onPrimaryContainer = Color(0xFF0A2A4F),
    secondary = Color(0xFF12795C),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFC9F2E4),
    onSecondaryContainer = Color(0xFF05291F),
    tertiary = Color(0xFF9A6B00),
    onTertiary = Color.White,
    background = Color(0xFFF6F8FA),
    onBackground = Color(0xFF12181F),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF12181F),
    surfaceVariant = Color(0xFFEDF1F5),
    onSurfaceVariant = Color(0xFF54606D),
    outline = Color(0xFFC3CCD6),
    error = Color(0xFFB3261E),
    onError = Color.White,
)

@Composable
fun CodexBridgeTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkScheme else LightScheme,
        content = content,
    )
}