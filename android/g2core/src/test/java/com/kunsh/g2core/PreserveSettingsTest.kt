package com.kunsh.g2core

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PreserveSettingsTest {
    @Test
    fun defaultStartupLeavesEvenSettingsAlone() = runBlocking {
        val g = G2Session(FakeTransport(null), CoroutineScope(coroutineContext + Job()), MagicAllocator(), { 0L }, -16,
            G2Session.Config(keepalive = false))
        val labels = g.startupPlan().map { it.label }
        for (l in G2_USER_SETTING_STEPS) assertFalse("$l must not run by default", l in labels)
        assertTrue("time sync" in labels)
        assertTrue("settings READ" in labels)
    }
}
