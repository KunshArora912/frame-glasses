package com.kunsh.frame

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The Claude API key, entered in the app or provisioned with
 * `adb shell am start -n com.kunsh.frame/.MainActivity --es claude_key <key>`.
 * Stored only on the phone: AES-256-GCM with a non-exportable Android Keystore key, ciphertext in a MODE_PRIVATE
 * prefs file (no extra dependency; androidx.security-crypto is deprecated). Never logged, never shown in full.
 */
class ApiKeyStore(ctx: Context) {
    companion object {
        private const val PREFS = "secrets"
        private const val ALIAS = "frame_claude_key"
        private const val K_DATA = "claude_key_enc"
        private const val K_LAST4 = "claude_key_last4"
        const val EXTRA = "claude_key"

        fun looksValid(k: String) = k.length in 20..300 && k.none { it.isWhitespace() }
    }

    private val prefs = ctx.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun secretKey(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build())
        return gen.generateKey()
    }

    @Synchronized
    fun set(key: String) {
        val k = key.trim()
        require(looksValid(k)) { "that does not look like an API key" }
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, secretKey())
        val enc = c.iv + c.doFinal(k.toByteArray(Charsets.UTF_8))
        prefs.edit().putString(K_DATA, Base64.encodeToString(enc, Base64.NO_WRAP)).putString(K_LAST4, k.takeLast(4)).apply()
    }

    @Synchronized
    fun get(): String? {
        val b64 = prefs.getString(K_DATA, null) ?: return null
        return try {
            val all = Base64.decode(b64, Base64.NO_WRAP)
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, all, 0, 12))
            String(c.doFinal(all, 12, all.size - 12), Charsets.UTF_8)
        } catch (_: Exception) { null }
    }

    fun clear() { prefs.edit().remove(K_DATA).remove(K_LAST4).apply() }

    val isSet: Boolean get() = prefs.contains(K_DATA)

    /** "key set (...abcd)" or "no key". */
    fun describe(): String = if (isSet) "key set (…${prefs.getString(K_LAST4, "????")})" else "no key"
}
