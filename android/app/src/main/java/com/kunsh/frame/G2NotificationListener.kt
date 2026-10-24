package com.kunsh.frame

import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Build
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.MutableLiveData

/**
 * Reads the phone's notifications (after the user grants "Notification access") and hands them to
 * [G2Link], which forwards them to the laptop while the G2 laptop link is on. Skips this app,
 * com.even.sg, ongoing/foreground-service notifications and group summaries; silent ones are sent
 * with "silent":true so the laptop's filter decides.
 */
class G2NotificationListener : NotificationListenerService() {

    companion object {
        val connected = MutableLiveData(false)

        fun hasAccess(ctx: Context): Boolean =
            NotificationManagerCompat.getEnabledListenerPackages(ctx).contains(ctx.packageName)
    }

    private val labels = HashMap<String, String>()

    override fun onListenerConnected() {
        connected.postValue(true)
        // The system rebinds us after a reboot or an app update: resume the laptop link if the user left it on
        // (notifications only; GPS needs the foreground service, which starts when the app is opened).
        val prefs = getSharedPreferences(FrameService.PREFS, Context.MODE_PRIVATE)
        if (prefs.getBoolean(FrameService.KEY_G2_ON, false) && !G2Link.wanted) {
            G2Link.start(prefs.getString(FrameService.KEY_G2_URL, G2Link.DEFAULT_URL) ?: G2Link.DEFAULT_URL)
        }
    }

    override fun onListenerDisconnected() { connected.postValue(false) }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName ?: return
        if (pkg == packageName || pkg in G2Link.NEVER || !G2Link.wanted) return
        val n = sbn.notification ?: return
        if (sbn.isOngoing || n.flags and Notification.FLAG_FOREGROUND_SERVICE != 0) return
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return
        val ex = n.extras
        val title = (ex.getCharSequence(Notification.EXTRA_TITLE_BIG) ?: ex.getCharSequence(Notification.EXTRA_TITLE))?.toString().orEmpty()
        val text = (ex.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: ex.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        if (title.isBlank() && text.isBlank()) return
        G2Link.sendNotif(pkg, label(pkg, n), title, text, sbn.postTime, sbn.key, false, isSilent(sbn))
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        val pkg = sbn.packageName ?: return
        if (pkg == packageName || pkg in G2Link.NEVER || !G2Link.wanted) return
        G2Link.sendRemoved(sbn.key)
    }

    private fun isSilent(sbn: StatusBarNotification): Boolean {
        val r = Ranking()
        val ranking = try { currentRanking } catch (_: Exception) { null } ?: return false
        if (!ranking.getRanking(sbn.key, r)) return false
        return r.importance in 1..NotificationManager.IMPORTANCE_LOW
    }

    private fun label(pkg: String, n: Notification): String = labels.getOrPut(pkg) {
        val pm = packageManager
        try {
            pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
        } catch (_: PackageManager.NameNotFoundException) {
            @Suppress("DEPRECATION")
            val ai: ApplicationInfo? = if (Build.VERSION.SDK_INT >= 33)
                n.extras.getParcelable("android.appInfo", ApplicationInfo::class.java)
            else n.extras.getParcelable("android.appInfo")
            ai?.let { pm.getApplicationLabel(it).toString() } ?: pkg
        }
    }
}
