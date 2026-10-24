package com.kunsh.frame

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlin.coroutines.resume

/**
 * One utterance from the PHONE's microphone through android.speech.SpeechRecognizer (the glasses' mic stream is LC3;
 * decoding it on Android is not done yet). Prefers the on-device recognizer (API 31+) and otherwise asks the default
 * recognizer to stay offline (EXTRA_PREFER_OFFLINE); whether a given phone honours that is up to its recognizer.
 */
class PhoneSpeech(private val ctx: Context, private val log: (String) -> Unit = {}) {

    data class Heard(val text: String?, val engine: String, val error: String? = null)

    @Volatile private var onDeviceBroken = false

    fun hasPermission() = ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    fun available(): Boolean = SpeechRecognizer.isRecognitionAvailable(ctx) ||
        (Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(ctx))

    fun engineName(): String = if (Build.VERSION.SDK_INT >= 31 && !onDeviceBroken && SpeechRecognizer.isOnDeviceRecognitionAvailable(ctx))
        "phone mic, on-device recognizer" else "phone mic, default recognizer (offline preferred)"

    /** Listen once (main thread inside). Cancelling the coroutine cancels the recognizer. */
    suspend fun listen(): Heard = withContext(Dispatchers.Main) {
        if (!hasPermission()) return@withContext Heard(null, "phone mic", "microphone permission not granted")
        val onDevice = Build.VERSION.SDK_INT >= 31 && !onDeviceBroken && SpeechRecognizer.isOnDeviceRecognitionAvailable(ctx)
        val first = once(onDevice)
        if (onDevice && first.text == null && first.error != null && first.error.startsWith("engine")) {
            log("on-device recognizer failed (${first.error}); using the default recognizer")
            onDeviceBroken = true
            return@withContext once(false)
        }
        first
    }

    private suspend fun once(onDevice: Boolean): Heard {
        val engine = if (onDevice) "phone mic, on-device" else "phone mic, default recognizer"
        val sr = if (onDevice && Build.VERSION.SDK_INT >= 31) SpeechRecognizer.createOnDeviceSpeechRecognizer(ctx)
        else SpeechRecognizer.createSpeechRecognizer(ctx)
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1200L)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, ctx.packageName)
        }
        return try {
            suspendCancellableCoroutine { cont ->
                cont.invokeOnCancellation { try { sr.cancel() } catch (_: Exception) { } }
                sr.setRecognitionListener(object : RecognitionListener {
                    override fun onResults(results: Bundle?) {
                        val t = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim()
                        if (cont.isActive) cont.resume(Heard(t?.ifEmpty { null }, engine, if (t.isNullOrEmpty()) "nothing recognised" else null))
                    }
                    override fun onError(error: Int) {
                        val why = when (error) {
                            SpeechRecognizer.ERROR_NO_MATCH -> "nothing recognised"
                            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "no speech heard"
                            SpeechRecognizer.ERROR_AUDIO -> "engine: audio error"
                            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "engine: no microphone permission"
                            SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "engine: needs the network"
                            SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "engine: recognizer busy"
                            SpeechRecognizer.ERROR_CLIENT -> "engine: client error"
                            SpeechRecognizer.ERROR_SERVER -> "engine: server error"
                            12 /* ERROR_LANGUAGE_NOT_SUPPORTED */, 13 /* ERROR_LANGUAGE_UNAVAILABLE */ -> "engine: language not available"
                            else -> "engine: error $error"
                        }
                        if (cont.isActive) cont.resume(Heard(null, engine, why))
                    }
                    override fun onReadyForSpeech(params: Bundle?) { log("speech: listening ($engine)") }
                    override fun onBeginningOfSpeech() {}
                    override fun onRmsChanged(rmsdB: Float) {}
                    override fun onBufferReceived(buffer: ByteArray?) {}
                    override fun onEndOfSpeech() {}
                    override fun onPartialResults(partialResults: Bundle?) {}
                    override fun onEvent(eventType: Int, params: Bundle?) {}
                })
                sr.startListening(intent)
            }
        } finally {
            try { sr.destroy() } catch (_: Exception) { }
        }
    }
}
