package com.kunsh.frame

import android.graphics.Color
import android.os.SystemClock
import android.view.View
import android.widget.Button
import android.widget.SeekBar
import android.widget.TextView
import androidx.appcompat.widget.SwitchCompat
import androidx.lifecycle.LifecycleOwner

/**
 * The G2 "Display" card: Display Height (vertical slider 0..20), Display Distance (Near / Mid / Far
 * plus a fine slider 0..20), brightness 1..50 with Auto, and the glasses' battery. In phone hub mode it reads and
 * applies the settings directly ([PhoneHub.applySet], g2core settings); in laptop mode it syncs both ways
 * through the laptop (g2/phone_link.py): incoming "display" messages move the controls unless the
 * user is dragging that control (or let go of it less than [HOLD_MS] ago); user changes send "set".
 */
class DisplayCard(root: View, owner: LifecycleOwner) {

    companion object {
        const val HOLD_MS = 1500L
        /** Near / Mid / Far presets on the 0..20 distance scale (fine-tune with the slider). */
        val PRESETS = linkedMapOf("Near" to 4, "Mid" to 10, "Far" to 16)
    }

    private val card: View = root.findViewById(R.id.displayCard)
    private val heightBar: SeekBar = root.findViewById(R.id.heightBar)
    private val heightVal: TextView = root.findViewById(R.id.heightVal)
    private val distanceBar: SeekBar = root.findViewById(R.id.distanceBar)
    private val distanceVal: TextView = root.findViewById(R.id.distanceVal)
    private val presetBtns: Map<String, Button> = mapOf(
        "Near" to root.findViewById(R.id.nearBtn), "Mid" to root.findViewById(R.id.midBtn), "Far" to root.findViewById(R.id.farBtn))
    private val brightBar: SeekBar = root.findViewById(R.id.brightBar)
    private val brightVal: TextView = root.findViewById(R.id.brightVal)
    private val autoSwitch: SwitchCompat = root.findViewById(R.id.autoSwitch)
    private val batteryText: TextView = root.findViewById(R.id.glassesBattery)

    private val dragging = HashSet<String>()
    private val holdUntil = HashMap<String, Long>()
    private var programmatic = false

    init {
        heightBar.max = 20
        distanceBar.max = 20
        brightBar.min = 1
        brightBar.max = 50
        bind(heightBar, "height") { heightVal.text = "$it" }
        bind(distanceBar, "distance") { distanceVal.text = "$it"; highlight(it) }
        bind(brightBar, "brightness") { brightVal.text = "$it" }
        for ((name, btn) in presetBtns) btn.setOnClickListener {
            val v = PRESETS.getValue(name)
            hold("distance")
            programmatic = true; distanceBar.progress = v; programmatic = false
            send("distance", v)
        }
        autoSwitch.setOnCheckedChangeListener { _, checked ->
            brightBar.isEnabled = !checked
            if (!programmatic) { hold("auto"); send("auto", if (checked) 1 else 0) }
        }
        G2Link.display.observe(owner) { if (!phoneMode()) show(it) }
        PhoneHub.display.observe(owner) { if (phoneMode()) show(it) }
        G2Link.state.observe(owner) { visibility() }
        PhoneHub.state.observe(owner) { visibility() }
    }

    /** Phone hub mode: read/apply directly through g2core; laptop mode: through g2/phone_link.py. */
    private fun phoneMode() = PhoneHub.wanted

    private fun send(key: String, value: Int) { if (phoneMode()) PhoneHub.applySet(key, value) else G2Link.sendSet(key, value) }

    private fun visibility() {
        card.visibility = if (G2Link.wanted || PhoneHub.wanted) View.VISIBLE else View.GONE
        if (PhoneHub.wanted && PhoneHub.display.value == null) show(null)
    }

    private fun bind(bar: SeekBar, key: String, label: (Int) -> Unit) {
        bar.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, p: Int, fromUser: Boolean) {
                label(p)
                if (fromUser && !programmatic) { hold(key); send(key, p) }
            }
            override fun onStartTrackingTouch(sb: SeekBar) { dragging += key }
            override fun onStopTrackingTouch(sb: SeekBar) {
                dragging -= key
                hold(key)
                send(key, sb.progress)                 // the final value, in case a drag value was coalesced away
            }
        })
    }

    private fun hold(key: String) { holdUntil[key] = SystemClock.uptimeMillis() + HOLD_MS }
    private fun free(key: String) = key !in dragging && SystemClock.uptimeMillis() >= (holdUntil[key] ?: 0L)

    private fun highlight(distance: Int) {
        val nearest = PRESETS.minByOrNull { kotlin.math.abs(it.value - distance) }?.key
        for ((name, btn) in presetBtns) btn.alpha = if (name == nearest) 1f else 0.45f
    }

    private fun show(d: G2Link.Display?) {
        if (d == null) {
            batteryText.text = if (phoneMode()) "Glasses: waiting for the phone hub to connect" else "Glasses: waiting for the laptop"
            batteryText.setTextColor(Color.parseColor("#999999"))
            return
        }
        programmatic = true
        try {
            if (free("height")) heightBar.progress = d.height
            if (free("distance")) distanceBar.progress = d.distance
            if (free("brightness")) brightBar.progress = d.brightness.coerceIn(1, 50)
            if (free("auto")) autoSwitch.isChecked = d.auto
        } finally { programmatic = false }
        heightVal.text = "${heightBar.progress}"
        distanceVal.text = "${distanceBar.progress}"
        brightVal.text = "${brightBar.progress}"
        highlight(distanceBar.progress)
        brightBar.isEnabled = !autoSwitch.isChecked
        batteryText.text = "Glasses battery ${d.battery}%" + if (d.charging) " (charging)" else ""
        batteryText.setTextColor(Color.parseColor(if (d.battery < 20 && !d.charging) "#EE3333" else "#EEEEEE"))
    }
}
