package com.kunsh.frame

import android.Manifest
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CalendarContract
import androidx.core.content.ContextCompat
import java.util.TimeZone

/**
 * [CalendarStore] over android.provider.CalendarContract (READ_CALENDAR / WRITE_CALENDAR). New events go to the
 * calendar chosen in the app (pref [KEY_CAL_ID]), else the primary writable one, else the first writable one.
 * Nothing here syncs anything itself; the calendar's own account does whatever it normally does.
 */
class AndroidCalendar(private val ctx: Context) : CalendarStore {

    data class Cal(val id: Long, val name: String, val account: String, val primary: Boolean)

    companion object {
        const val KEY_CAL_ID = "calendar_id"

        fun canRead(ctx: Context) =
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED
        fun canWrite(ctx: Context) =
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.WRITE_CALENDAR) == PackageManager.PERMISSION_GRANTED
    }

    private val prefs get() = ctx.getSharedPreferences(FrameService.PREFS, Context.MODE_PRIVATE)

    /** Visible calendars the user can add events to (contributor access or better). */
    fun writableCalendars(): List<Cal> {
        if (!canRead(ctx)) return emptyList()
        val out = ArrayList<Cal>()
        val proj = arrayOf(CalendarContract.Calendars._ID, CalendarContract.Calendars.CALENDAR_DISPLAY_NAME,
            CalendarContract.Calendars.ACCOUNT_NAME, CalendarContract.Calendars.IS_PRIMARY)
        val sel = "${CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL} >= ? AND ${CalendarContract.Calendars.VISIBLE} = 1"
        ctx.contentResolver.query(CalendarContract.Calendars.CONTENT_URI, proj, sel,
            arrayOf(CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR.toString()), null)?.use { c ->
            while (c.moveToNext()) out.add(Cal(c.getLong(0), c.getString(1) ?: "calendar", c.getString(2) ?: "", c.getInt(3) == 1))
        }
        return out
    }

    fun chosen(): Cal? {
        val all = writableCalendars()
        val id = prefs.getLong(KEY_CAL_ID, -1L)
        return all.firstOrNull { it.id == id } ?: all.firstOrNull { it.primary } ?: all.firstOrNull()
    }

    fun choose(id: Long) { prefs.edit().putLong(KEY_CAL_ID, id).apply() }

    override fun add(e: CalendarStore.Event): String {
        if (!canWrite(ctx)) throw IllegalStateException("calendar write permission not granted on the phone")
        val cal = chosen() ?: throw IllegalStateException("no writable calendar on the phone")
        val v = ContentValues().apply {
            put(CalendarContract.Events.CALENDAR_ID, cal.id)
            put(CalendarContract.Events.TITLE, e.title)
            put(CalendarContract.Events.DTSTART, e.startMs)
            put(CalendarContract.Events.DTEND, e.endMs)
            put(CalendarContract.Events.ALL_DAY, if (e.allDay) 1 else 0)
            put(CalendarContract.Events.EVENT_TIMEZONE, if (e.allDay) "UTC" else TimeZone.getDefault().id)
            if (e.location.isNotEmpty()) put(CalendarContract.Events.EVENT_LOCATION, e.location)
        }
        ctx.contentResolver.insert(CalendarContract.Events.CONTENT_URI, v) ?: throw IllegalStateException("calendar insert failed")
        return cal.name
    }

    override fun list(fromMs: Long, toMs: Long): List<CalendarStore.Event> {
        if (!canRead(ctx)) throw IllegalStateException("calendar read permission not granted on the phone")
        val b = CalendarContract.Instances.CONTENT_URI.buildUpon()
        ContentUris.appendId(b, fromMs)
        ContentUris.appendId(b, toMs)
        val proj = arrayOf(CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN, CalendarContract.Instances.END,
            CalendarContract.Instances.ALL_DAY, CalendarContract.Instances.EVENT_LOCATION, CalendarContract.Instances.CALENDAR_DISPLAY_NAME)
        val out = ArrayList<CalendarStore.Event>()
        ctx.contentResolver.query(b.build(), proj, "${CalendarContract.Instances.VISIBLE} = 1", null,
            "${CalendarContract.Instances.BEGIN} ASC")?.use { c ->
            while (c.moveToNext()) {
                out.add(CalendarStore.Event(c.getString(0) ?: "(no title)", c.getLong(1), c.getLong(2), c.getInt(3) == 1,
                    c.getString(4) ?: "", c.getString(5) ?: ""))
            }
        }
        return out
    }
}
