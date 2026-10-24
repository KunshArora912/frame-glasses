package com.kunsh.frame

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Looper
import androidx.core.content.ContextCompat

/**
 * GPS for the G2 laptop link through android.location.LocationManager (no Play Services): GPS and
 * network providers, at most one fix forwarded per interval. A coarse network fix is skipped while a
 * recent GPS fix is much more accurate. Runs only while FrameService is in the foreground.
 */
class G2Location(private val ctx: Context, private val onFix: (Location) -> Unit) {

    private val lm = ctx.getSystemService(Context.LOCATION_SERVICE) as LocationManager
    private var intervalMs = 5000L
    private var lastSent: Location? = null
    private var running = false

    private val listener = object : LocationListener {
        override fun onLocationChanged(location: Location) = consider(location)
        override fun onProviderEnabled(provider: String) {}
        override fun onProviderDisabled(provider: String) {}
        @Deprecated("API < 29") override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    }

    fun hasPermission(): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

    /** Returns a short description of what started, or why nothing did. */
    @SuppressLint("MissingPermission")
    fun start(intervalMs: Long): String {
        stop()
        this.intervalMs = intervalMs
        if (!hasPermission()) return "no location permission"
        val providers = listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER).filter { it in lm.allProviders }
        if (providers.isEmpty()) return "no location provider"
        for (p in providers) {
            try {
                lm.requestLocationUpdates(p, intervalMs, 0f, listener, Looper.getMainLooper())
                lm.getLastKnownLocation(p)?.let { consider(it) }
            } catch (e: SecurityException) {
                return "location refused: ${e.message}"
            } catch (e: IllegalArgumentException) { /* provider vanished */ }
        }
        running = true
        val off = providers.filter { !lm.isProviderEnabled(it) }
        return "GPS every ${intervalMs / 1000}s (${providers.joinToString("+")})" + if (off.isNotEmpty()) "; off in settings: ${off.joinToString()}" else ""
    }

    fun stop() {
        if (running) try { lm.removeUpdates(listener) } catch (_: Exception) {}
        running = false
    }

    private fun consider(loc: Location) {
        val prev = lastSent
        if (prev != null) {
            val dt = loc.time - prev.time
            if (dt < 0) return
            if (dt < intervalMs * 9 / 10) {
                // inside the interval: wait for the next one
                return
            }
            if (loc.provider == LocationManager.NETWORK_PROVIDER && prev.provider == LocationManager.GPS_PROVIDER &&
                dt < intervalMs * 3 && loc.accuracy > prev.accuracy * 2) return
        }
        lastSent = loc
        onFix(loc)
    }
}
