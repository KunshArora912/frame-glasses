package com.kunsh.frame

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.provider.Settings
import android.text.format.DateFormat
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.RadioGroup
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private var svc: FrameService? = null
    private var bound = false
    private var pendingConnect = false

    private lateinit var bleText: TextView
    private lateinit var connectBtn: Button
    private lateinit var disconnectBtn: Button
    private lateinit var forgetBtn: Button
    private lateinit var pctText: TextView
    private lateinit var stateText: TextView
    private lateinit var fwText: TextView
    private lateinit var seenText: TextView
    private lateinit var bar: ProgressBar
    private lateinit var relayText: TextView
    private lateinit var relayBtn: Button
    private lateinit var relayUrl: EditText
    private lateinit var appsRow: LinearLayout
    private lateinit var luaInput: EditText
    private lateinit var logText: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var g2Text: TextView
    private lateinit var g2Btn: Button
    private lateinit var g2Url: EditText
    private lateinit var g2AccessText: TextView
    private lateinit var g2NotifText: TextView
    private lateinit var g2FixText: TextView
    private var pendingG2 = false
    private var pendingPhoneHub = false
    private lateinit var hubMode: RadioGroup
    private lateinit var phoneHubText: TextView
    private lateinit var phoneHubArms: TextView
    private lateinit var phoneHubBtn: Button
    private lateinit var keyInput: EditText
    private lateinit var keyText: TextView
    private lateinit var micText: TextView
    private lateinit var calText: TextView
    private lateinit var ideasCountText: TextView

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            svc = (binder as FrameService.LocalBinder).service
            bound = true
            observe()
            if (pendingConnect) { pendingConnect = false; svc?.connect() }
            if (pendingG2) { pendingG2 = false; svc?.startG2(g2Url.text.toString()) } else svc?.resumeG2IfWanted()
            if (pendingPhoneHub) { pendingPhoneHub = false; svc?.startPhoneHub() } else svc?.resumePhoneHubIfWanted()
        }
        override fun onServiceDisconnected(name: ComponentName) { svc = null; bound = false }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        bleText = findViewById(R.id.bleText)
        connectBtn = findViewById(R.id.connectBtn)
        disconnectBtn = findViewById(R.id.disconnectBtn)
        forgetBtn = findViewById(R.id.forgetBtn)
        pctText = findViewById(R.id.pctText)
        stateText = findViewById(R.id.stateText)
        fwText = findViewById(R.id.fwText)
        seenText = findViewById(R.id.seenText)
        bar = findViewById(R.id.bar)
        relayText = findViewById(R.id.relayText)
        relayBtn = findViewById(R.id.relayBtn)
        relayUrl = findViewById(R.id.relayUrl)
        appsRow = findViewById(R.id.appsRow)
        luaInput = findViewById(R.id.luaInput)
        logText = findViewById(R.id.logText)
        logScroll = findViewById(R.id.logScroll)
        g2Text = findViewById(R.id.g2Text)
        g2Btn = findViewById(R.id.g2Btn)
        g2Url = findViewById(R.id.g2Url)
        g2AccessText = findViewById(R.id.g2AccessText)
        g2NotifText = findViewById(R.id.g2NotifText)
        g2FixText = findViewById(R.id.g2FixText)
        g2Btn.setOnClickListener { if (G2Link.wanted) svc?.stopG2() else startG2WithPermissions() }
        findViewById<Button>(R.id.g2AccessBtn).setOnClickListener { openNotificationAccess() }
        observeG2()
        PhoneHub.init(applicationContext)
        setupPhoneHub()
        handleKeyIntent(intent)
        DisplayCard(findViewById(android.R.id.content), this)

        connectBtn.setOnClickListener { connectWithPermissions() }
        disconnectBtn.setOnClickListener { svc?.disconnect() }
        forgetBtn.setOnClickListener { svc?.forgetDevice() }
        relayBtn.setOnClickListener {
            val s = svc ?: return@setOnClickListener
            if (s.relay.wanted) s.unlinkRelay() else s.linkRelay(relayUrl.text.toString().trim())
        }
        findViewById<Button>(R.id.sendBtn).setOnClickListener {
            val t = luaInput.text.toString()
            if (t.isNotBlank()) { svc?.sendLua(t); luaInput.setText("") }
        }
        findViewById<Button>(R.id.breakBtn).setOnClickListener { svc?.sendBreak() }
        findViewById<Button>(R.id.resetBtn).setOnClickListener { svc?.sendReset() }
        findViewById<Button>(R.id.clearBtn).setOnClickListener { logText.text = "" }

        for (id in FrameService.APPS) {
            val b = Button(this)
            b.text = id
            b.isAllCaps = false
            b.setOnClickListener { svc?.askLaunch(id) }
            appsRow.addView(b)
        }

        val intent = Intent(this, FrameService::class.java)
        startService(intent)   // plain start keeps it alive after unbind; it goes foreground when a link exists
        bindService(intent, conn, Context.BIND_AUTO_CREATE)
    }

    override fun onResume() {
        super.onResume()
        refreshG2Access()
        refreshPhoneHubInfo()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleKeyIntent(intent)
    }

    // ---------------------------------------------------------- G2 phone hub
    /** `adb shell am start -n com.kunsh.frame/.MainActivity --es claude_key <key>`: store it, then strip it from the intent. */
    private fun handleKeyIntent(i: Intent?) {
        val k = i?.getStringExtra(ApiKeyStore.EXTRA) ?: return
        i.removeExtra(ApiKeyStore.EXTRA)
        setIntent(i)
        try { PhoneHub.keys.set(k); toast("Claude key saved") } catch (e: Exception) { toast("Claude key not saved: ${e.message}") }
        refreshPhoneHubInfo()
    }

    private fun toast(s: String) = android.widget.Toast.makeText(this, s, android.widget.Toast.LENGTH_SHORT).show()

    private fun setupPhoneHub() {
        hubMode = findViewById(R.id.hubMode)
        phoneHubText = findViewById(R.id.phoneHubText)
        phoneHubArms = findViewById(R.id.phoneHubArms)
        phoneHubBtn = findViewById(R.id.phoneHubBtn)
        keyInput = findViewById(R.id.keyInput)
        keyText = findViewById(R.id.keyText)
        micText = findViewById(R.id.micText)
        calText = findViewById(R.id.calText)
        ideasCountText = findViewById(R.id.ideasCountText)

        hubMode.check(if (PhoneHub.mode == "phone") R.id.hubPhone else R.id.hubLaptop)
        hubMode.setOnCheckedChangeListener { _, id ->
            val m = if (id == R.id.hubPhone) "phone" else "laptop"
            val s = svc
            if (s != null) s.setHubMode(m) else PhoneHub.mode = m
            applyHubMode()
        }
        applyHubMode()

        phoneHubBtn.setOnClickListener { if (PhoneHub.wanted) svc?.stopPhoneHub() else startPhoneHubWithPermissions() }
        findViewById<Button>(R.id.phoneAccessBtn).setOnClickListener { openNotificationAccess() }
        val notifSwitch = findViewById<SwitchCompat>(R.id.phoneNotifSwitch)
        notifSwitch.isChecked = PhoneHub.notificationsOn
        notifSwitch.setOnCheckedChangeListener { _, on -> PhoneHub.notificationsOn = on }

        findViewById<Button>(R.id.keySaveBtn).setOnClickListener {
            val k = keyInput.text.toString().trim()
            if (k.isEmpty()) return@setOnClickListener
            try { PhoneHub.keys.set(k); keyInput.setText(""); toast("Claude key saved") } catch (e: Exception) { toast(e.message ?: "not saved") }
            refreshPhoneHubInfo()
        }
        findViewById<Button>(R.id.keyClearBtn).setOnClickListener { PhoneHub.keys.clear(); refreshPhoneHubInfo() }

        val feats = listOf(
            Triple(R.id.featStocks, PhoneHub.KEY_FEAT_STOCKS, R.id.featStocksText),
            Triple(R.id.featCalendar, PhoneHub.KEY_FEAT_CAL, R.id.featCalendarText),
            Triple(R.id.featIdeas, PhoneHub.KEY_FEAT_IDEAS, R.id.featIdeasText))
        for ((sw, key, _) in feats) {
            val v = findViewById<SwitchCompat>(sw)
            v.isChecked = PhoneHub.feature(key)
            v.setOnCheckedChangeListener { _, on ->
                if (on && key == PhoneHub.KEY_FEAT_CAL && !AndroidCalendar.canRead(this)) requestCalendar()
                PhoneHub.setFeature(key, on)
            }
        }
        PhoneHub.feeds.observe(this) { m ->
            for ((_, key, tv) in feats) {
                findViewById<TextView>(tv).text = m[key] ?: if (PhoneHub.feature(key)) "on" else "off"
            }
        }
        val tickers = findViewById<EditText>(R.id.tickersInput)
        tickers.setText(PhoneHub.tickers.joinToString(", "))
        findViewById<Button>(R.id.tickersSaveBtn).setOnClickListener {
            PhoneHub.tickers = tickers.text.toString().split(',', ' ').mapNotNull { Stocks.normalize(it) }.distinct()
            tickers.setText(PhoneHub.tickers.joinToString(", "))
            PhoneHub.refreshStocksNow()
        }
        findViewById<Button>(R.id.calChooseBtn).setOnClickListener { chooseCalendar() }

        PhoneHub.state.observe(this) { st ->
            phoneHubText.text = st.text
            phoneHubText.setTextColor(Color.parseColor(if (st.ready) "#33CC33" else "#999999"))
            phoneHubBtn.text = if (PhoneHub.wanted) "Disconnect" else "Connect"
            val bat = st.battery?.let { "$it%" + if (st.charging == true) " (charging)" else "" } ?: "-"
            phoneHubArms.text = "Left: ${st.left}   Right: ${st.right}   Battery: $bat"
        }
        PhoneHub.ai.observe(this) { findViewById<TextView>(R.id.aiText).text = it }
        PhoneHub.lastNotif.observe(this) { findViewById<TextView>(R.id.phoneNotifText).text = "Last notification: $it" }
        refreshPhoneHubInfo()
    }

    private fun applyHubMode() {
        val phone = PhoneHub.mode == "phone"
        findViewById<View>(R.id.g2LaptopCard).visibility = if (phone) View.GONE else View.VISIBLE
        for (id in listOf(R.id.phoneHubCard, R.id.askCard, R.id.featuresCard))
            findViewById<View>(id).visibility = if (phone) View.VISIBLE else View.GONE
    }

    private fun refreshPhoneHubInfo() {
        if (!::keyText.isInitialized) return
        keyText.text = "Claude API: " + PhoneHub.keys.describe()
        val mic = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        micText.text = "Questions are heard by the PHONE microphone (${PhoneHub.speechEngine}), not the glasses' mic." +
            if (mic) "" else " Microphone permission NOT granted."
        val cal = if (AndroidCalendar.canRead(this)) PhoneHub.calendar.chosen()?.let { "${it.name} (${it.account})" } ?: "no writable calendar"
        else "permission not granted"
        calText.text = "Calendar for new events: $cal"
        ideasCountText.text = "Ideas saved on this phone: ${PhoneHub.ideas.all().size}"
    }

    private fun chooseCalendar() {
        if (!AndroidCalendar.canRead(this)) { requestCalendar(); return }
        val cals = PhoneHub.calendar.writableCalendars()
        if (cals.isEmpty()) { toast("No writable calendar on this phone"); return }
        AlertDialog.Builder(this)
            .setTitle("Calendar for new events")
            .setItems(cals.map { "${it.name} (${it.account})" + if (it.primary) " - primary" else "" }.toTypedArray()) { _, i ->
                PhoneHub.calendar.choose(cals[i].id); refreshPhoneHubInfo()
            }
            .show()
    }

    private fun requestCalendar() {
        ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR), 4)
    }

    private fun phoneHubPermissions(): List<String> {
        val p = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 31) { p += Manifest.permission.BLUETOOTH_SCAN; p += Manifest.permission.BLUETOOTH_CONNECT }
        else p += Manifest.permission.ACCESS_FINE_LOCATION
        p += Manifest.permission.RECORD_AUDIO
        p += Manifest.permission.READ_CALENDAR
        p += Manifest.permission.WRITE_CALENDAR
        if (Build.VERSION.SDK_INT >= 33) p += Manifest.permission.POST_NOTIFICATIONS
        return p.filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
    }

    private fun bluetoothGranted(): Boolean {
        val need = if (Build.VERSION.SDK_INT >= 31) listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        else listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        return need.all { ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED }
    }

    private fun startPhoneHubWithPermissions() {
        val missing = phoneHubPermissions()
        if (missing.isEmpty()) startPhoneHubNow() else ActivityCompat.requestPermissions(this, missing.toTypedArray(), 3)
    }

    private fun startPhoneHubNow() {
        if (svc != null) svc?.startPhoneHub() else pendingPhoneHub = true
    }

    override fun onDestroy() {
        if (bound) unbindService(conn)
        super.onDestroy()
    }

    private fun observe() {
        val s = svc ?: return
        relayUrl.setText(s.savedRelayUrl)
        g2Url.setText(s.savedG2Url)
        s.bleState.observe(this) { st ->
            bleText.text = if (st.connected) "Connected: ${st.name ?: "Frame"}" else "Not connected"
            bleText.setTextColor(if (st.connected) Color.parseColor("#33CC33") else Color.parseColor("#999999"))
            connectBtn.visibility = if (st.connected) View.GONE else View.VISIBLE
            disconnectBtn.visibility = if (st.connected) View.VISIBLE else View.GONE
            if (!st.connected) { stateText.text = "glasses asleep or out of range" }
        }
        s.status.observe(this) { st ->
            pctText.text = if (st.pct != null) "${st.pct}%" else "--%"
            bar.progress = st.pct ?: 0
            val color = when { (st.pct ?: 100) < 20 -> "#EE3333"; (st.pct ?: 100) < 50 -> "#FF9900"; else -> "#33CC33" }
            bar.progressTintList = android.content.res.ColorStateList.valueOf(Color.parseColor(color))
            stateText.text = when (st.charging) {
                true -> if ((st.pct ?: 0) >= 99) "Charged" else "Charging"
                false -> "On battery"
                null -> ""
            }
            fwText.text = st.fw ?: ""
            seenText.text = "reported " + DateFormat.format("HH:mm:ss", st.seenAt)
        }
        s.relayState.observe(this) { linked ->
            relayText.text = if (linked) "Laptop linked" else "Laptop relay"
            relayText.setTextColor(if (linked) Color.parseColor("#33CC33") else Color.parseColor("#EEEEEE"))
            relayBtn.text = if (s.relay.wanted) "Unlink" else "Link"
        }
        s.log.observe(this) { lines ->
            logText.text = lines.joinToString("\n")
            logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    // ------------------------------------------------------- G2 laptop link
    private fun observeG2() {
        G2Link.state.observe(this) { st ->
            g2Text.text = st.text
            g2Text.setTextColor(Color.parseColor(if (st.linked) "#33CC33" else "#999999"))
            g2Btn.text = if (G2Link.wanted) "Disconnect" else "Connect"
        }
        G2Link.lastNotif.observe(this) { g2NotifText.text = "Last notification: $it" }
        G2Link.lastFix.observe(this) { g2FixText.text = "Last GPS fix: $it" }
        G2NotificationListener.connected.observe(this) { refreshG2Access() }
    }

    private fun hasLocation(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

    private fun refreshG2Access() {
        if (!::g2AccessText.isInitialized) return
        val access = G2NotificationListener.hasAccess(this)
        g2AccessText.text = "Notification access: ${if (access) "granted" else "NOT granted"}\n" +
            "Location: ${if (hasLocation()) "granted" else "not granted"}"
        g2AccessText.setTextColor(Color.parseColor(if (access) "#EEEEEE" else "#FF9900"))
    }

    private fun openNotificationAccess() {
        val detail = if (Build.VERSION.SDK_INT >= 30) Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS)
            .putExtra(Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                ComponentName(this, G2NotificationListener::class.java).flattenToString()) else null
        try { startActivity(detail ?: Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
        catch (_: Exception) { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
    }

    private fun g2Permissions(): List<String> {
        val p = arrayListOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
        if (Build.VERSION.SDK_INT >= 33) p += Manifest.permission.POST_NOTIFICATIONS
        return p.filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
    }

    private fun startG2WithPermissions() {
        val missing = g2Permissions()
        if (missing.isEmpty()) startG2Now()
        else ActivityCompat.requestPermissions(this, missing.toTypedArray(), 2)   // location is optional: start either way
    }

    private fun startG2Now() {
        if (svc != null) svc?.startG2(g2Url.text.toString()) else pendingG2 = true
    }

    // ---------------------------------------------------------- permissions
    private fun neededPermissions(): List<String> {
        val p = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 31) {
            p += Manifest.permission.BLUETOOTH_SCAN
            p += Manifest.permission.BLUETOOTH_CONNECT
        } else {
            p += Manifest.permission.ACCESS_FINE_LOCATION
        }
        if (Build.VERSION.SDK_INT >= 33) p += Manifest.permission.POST_NOTIFICATIONS
        return p.filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
    }

    private fun connectWithPermissions() {
        val missing = neededPermissions()
        if (missing.isEmpty()) {
            if (svc != null) svc?.connect() else pendingConnect = true
        } else {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), 1)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 2) { refreshG2Access(); startG2Now(); return }
        if (requestCode == 3) {
            refreshPhoneHubInfo()
            if (bluetoothGranted()) startPhoneHubNow() else toast("Bluetooth permission is needed to reach the glasses")
            return
        }
        if (requestCode == 4) { refreshPhoneHubInfo(); return }
        if (requestCode == 1 && neededPermissions().none { it != Manifest.permission.POST_NOTIFICATIONS }) {
            svc?.connect()
        }
    }
}
