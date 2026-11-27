package com.portaldash.shell

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.TextUtils
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.Manifest
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import java.io.ByteArrayInputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.NetworkInterface
import java.net.Inet4Address
import java.net.URL
import java.security.MessageDigest
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Full-screen WebView that shows the dashboard.
 *
 * By default it serves the files bundled in the APK. A different URL can be set with
 *   adb shell am start -n com.portaldash.shell/.MainActivity --es url http://host:port/
 * and cleared again with `--es url default`. The choice is remembered across restarts.
 *
 * With `--es live http://host:port/dashboard/ --es livekey KEY` (scripts/live.sh) each
 * bundled file is first asked for there and used if it is signed with KEY, so the
 * dashboard can be updated over Wi-Fi; `--es live off` goes back to the bundled files.
 */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private lateinit var prefs: SharedPreferences

    private val handler = Handler(Looper.getMainLooper())
    private val retryLoad = Runnable { webView.loadUrl(startUrl()) }

    private val bleRelay by lazy { BleRelay(this) }

    // While the live server does not answer, bundled files are used without asking again.
    @Volatile
    private var liveDownUntil = 0L

    private var cornerTaps = 0
    private var firstCornerTapAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("shell", MODE_PRIVATE)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // Debug builds can be inspected from chrome://inspect on the host.
        val debuggable = applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0
        WebView.setWebContentsDebuggingEnabled(debuggable)

        webView = WebView(this).apply {
            setBackgroundColor(Color.BLACK)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            // The bundled dashboard is served over https but reads plain-http servers on the LAN.
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            webViewClient = ShellClient()
            webChromeClient = MicrophoneClient()
            addJavascriptInterface(Bridge(), "PortalShell")
        }
        setContentView(webView)

        applyHomeExtra(intent)
        applyUrlExtra(intent)
        applyPairExtra(intent)
        applyLiveExtra(intent)
        webView.loadUrl(startUrl())
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        applyHomeExtra(intent)
        applyPairExtra(intent)
        if (applyLiveExtra(intent) or applyUrlExtra(intent)) {
            handler.removeCallbacks(retryLoad)
            webView.loadUrl(startUrl())
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemBars()
    }

    override fun onDestroy() {
        handler.removeCallbacks(retryLoad)
        bleRelay.stop()
        webView.destroy()
        super.onDestroy()
    }

    // The shell is meant to be the home screen, so Back has nowhere to go.
    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack()
    }

    /**
     * Escape hatch that does not depend on the page: five quick taps in the top-left
     * corner open the Portal's Settings (needed to re-enable ADB after a reboot).
     */
    override fun dispatchTouchEvent(event: MotionEvent): Boolean {
        if (event.actionMasked == MotionEvent.ACTION_DOWN) {
            val corner = CORNER_DP * resources.displayMetrics.density
            if (event.x < corner && event.y < corner) {
                val now = event.eventTime
                if (now - firstCornerTapAt > CORNER_WINDOW_MS) {
                    firstCornerTapAt = now
                    cornerTaps = 0
                }
                if (++cornerTaps >= CORNER_TAPS) {
                    cornerTaps = 0
                    openSettings()
                }
            } else {
                cornerTaps = 0
            }
        }
        return super.dispatchTouchEvent(event)
    }

    /** Stores or clears the URL override carried by [intent]; returns true if it had one. */
    private fun applyUrlExtra(intent: Intent?): Boolean {
        val url = intent?.getStringExtra(EXTRA_URL)?.trim() ?: return false
        if (url.startsWith("http://") || url.startsWith("https://")) {
            prefs.edit().putString(PREF_URL, url).apply()
        } else {
            prefs.edit().remove(PREF_URL).apply()
        }
        return true
    }

    /**
     * Turns the home-screen entry point (the .Home alias) on or off when started with
     * `--ez home true|false`. adb cannot enable a component of an installed app itself.
     */
    private fun applyHomeExtra(intent: Intent?) {
        if (intent?.hasExtra(EXTRA_HOME) != true) return
        val state = if (intent.getBooleanExtra(EXTRA_HOME, false)) {
            PackageManager.COMPONENT_ENABLED_STATE_ENABLED
        } else {
            PackageManager.COMPONENT_ENABLED_STATE_DEFAULT
        }
        packageManager.setComponentEnabledSetting(
            ComponentName(this, "$packageName.Home"),
            state,
            PackageManager.DONT_KILL_APP,
        )
    }

    /**
     * Started with `--es pair host:port` by muse/muse.py: lets the Mac at that address use
     * this device's Bluetooth to pair with the Muse app, until the Mac hangs up.
     */
    private fun applyPairExtra(intent: Intent?) {
        val target = intent?.getStringExtra(EXTRA_PAIR)?.trim() ?: return
        bleRelay.start(target)
    }

    /** Stores or clears the live-update server carried by [intent]; returns true if it had one. */
    private fun applyLiveExtra(intent: Intent?): Boolean {
        val live = intent?.getStringExtra(EXTRA_LIVE)?.trim() ?: return false
        val key = intent.getStringExtra(EXTRA_LIVE_KEY)?.trim().orEmpty()
        if (live.startsWith("http://") && live.endsWith("/") && key.isNotEmpty()) {
            prefs.edit().putString(PREF_LIVE, live).putString(PREF_LIVE_KEY, key).apply()
        } else {
            prefs.edit().remove(PREF_LIVE).remove(PREF_LIVE_KEY).apply()
        }
        liveDownUntil = 0
        return true
    }

    /**
     * The live server's copy of a dashboard file, or null to use the bundled one: when no
     * server is set, it does not answer, it lacks the file, or the signature is wrong.
     */
    private fun liveFile(path: String): ByteArray? {
        val live = prefs.getString(PREF_LIVE, null) ?: return null
        val key = prefs.getString(PREF_LIVE_KEY, null) ?: return null
        // The household's settings always come from the APK; subfolders are not served.
        if (path == "config.js" || path.contains('/')) return null
        if (System.currentTimeMillis() < liveDownUntil) return null
        return try {
            val connection = URL(live + path).openConnection() as HttpURLConnection
            connection.connectTimeout = LIVE_TIMEOUT_MS
            connection.readTimeout = LIVE_TIMEOUT_MS
            connection.useCaches = false
            try {
                if (connection.responseCode != 200) return null
                val body = connection.inputStream.use { it.readBytes() }
                val mac = Mac.getInstance("HmacSHA256")
                mac.init(SecretKeySpec(key.toByteArray(), "HmacSHA256"))
                mac.update(path.toByteArray())
                mac.update('\n'.code.toByte())
                val expected = mac.doFinal(body).joinToString("") { "%02x".format(it) }
                val given = connection.getHeaderField("X-Signature").orEmpty()
                if (MessageDigest.isEqual(expected.toByteArray(), given.toByteArray())) body else null
            } finally {
                connection.disconnect()
            }
        } catch (e: IOException) {
            liveDownUntil = System.currentTimeMillis() + LIVE_RETRY_MS
            null
        }
    }

    private fun startUrl(): String = prefs.getString(PREF_URL, null) ?: BUNDLED_URL

    private fun openSettings() {
        try {
            startActivity(Intent(Settings.ACTION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: ActivityNotFoundException) {
            // Nothing to open on this device.
        }
    }

    @Suppress("DEPRECATION") // WindowInsetsController needs API 30; Portals stop at 29.
    private fun hideSystemBars() {
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or
            View.SYSTEM_UI_FLAG_FULLSCREEN or
            View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
            View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or
            View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
    }

    private inner class ShellClient : WebViewClient() {

        // Serve bundled assets from an https origin so fetch() and ES modules work,
        // which they do not from file:// URLs.
        override fun shouldInterceptRequest(
            view: WebView,
            request: WebResourceRequest,
        ): WebResourceResponse? {
            val url = request.url
            if (url.host != ASSET_HOST) return null
            val path = url.path.orEmpty().trimStart('/').ifEmpty { "index.html" }
            liveFile(path)?.let {
                return WebResourceResponse(
                    mimeType(path), null, 200, "OK",
                    mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(it),
                )
            }
            return try {
                WebResourceResponse(mimeType(path), null, assets.open(path))
            } catch (e: IOException) {
                WebResourceResponse("text/plain", "utf-8", 404, "Not Found", emptyMap(), null)
            }
        }

        override fun onReceivedError(
            view: WebView,
            request: WebResourceRequest,
            error: WebResourceError,
        ) {
            if (!request.isForMainFrame) return
            // A remote dashboard may simply not be up yet (e.g. after a power cut); keep trying.
            val target = TextUtils.htmlEncode(request.url.toString())
            val reason = TextUtils.htmlEncode(error.description.toString())
            view.loadDataWithBaseURL(
                null,
                "<body style='margin:0;height:100vh;display:grid;place-items:center;" +
                    "background:#000;color:#8b93a3;font:24px sans-serif;text-align:center'>" +
                    "<div>Can't reach $target<br>$reason<br><br>Retrying…</div></body>",
                "text/html",
                "utf-8",
                null,
            )
            handler.removeCallbacks(retryLoad)
            handler.postDelayed(retryLoad, RETRY_MS)
        }
    }

    /**
     * Lets the bundled dashboard (and the dev server) use the microphone and the camera,
     * and nothing else.
     */
    private inner class MicrophoneClient : WebChromeClient() {
        override fun onPermissionRequest(request: PermissionRequest) {
            if (request.origin.host !in MICROPHONE_HOSTS) {
                request.deny()
                return
            }
            // What the page asked for, with the Android permission each one rests on.
            val wanted = request.resources.mapNotNull { resource -> CAPTURE[resource]?.let { resource to it } }
            val allowed = wanted.filter { allowed(it.second) }.map { it.first }
            if (wanted.isNotEmpty() && allowed.size == wanted.size) {
                request.grant(allowed.toTypedArray())
            } else {
                // scripts/install.sh grants these; ask in case it was installed another way.
                val missing = wanted.map { it.second }.filterNot { allowed(it) }
                if (missing.isNotEmpty()) requestPermissions(missing.toTypedArray(), 0)
                request.deny()
            }
        }
    }

    private fun allowed(permission: String) =
        checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

    /** Exposed to the page as `window.PortalShell`. */
    private inner class Bridge {

        /** Screen brightness for this window, 0.0–1.0. */
        @JavascriptInterface
        fun setBrightness(level: Float) = setWindowBrightness(level.coerceIn(0.01f, 1f))

        /** Hand brightness back to the system setting. */
        @JavascriptInterface
        fun resetBrightness() =
            setWindowBrightness(WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE)

        @JavascriptInterface
        fun openSettings() = runOnUiThread { this@MainActivity.openSettings() }

        @JavascriptInterface
        fun reload() = runOnUiThread { webView.loadUrl(startUrl()) }

        /** Whether the page may open the camera (snapshot.js shows its button if so). */
        @JavascriptInterface
        fun hasCamera(): Boolean = allowed(Manifest.permission.CAMERA)

        /** This device's IPv4 addresses, comma-separated: where to look for the Mac. */
        @JavascriptInterface
        fun localAddresses(): String = try {
            NetworkInterface.getNetworkInterfaces().asSequence()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.inetAddresses.asSequence() }
                .filterIsInstance<Inet4Address>()
                .mapNotNull { it.hostAddress }
                .joinToString(",")
        } catch (e: IOException) {
            ""
        }

        /**
         * The dashboard found the Mac at a new address: ask for live files there from now
         * on. They are still only used when signed with the key the Mac gave over USB.
         */
        @JavascriptInterface
        fun serverMoved(host: String) {
            if (!Regex("""\d{1,3}(\.\d{1,3}){3}""").matches(host)) return
            val live = prefs.getString(PREF_LIVE, null) ?: return
            val old = URL(live)
            prefs.edit().putString(PREF_LIVE, "http://$host:${old.port}${old.path}").apply()
            liveDownUntil = 0
        }

        private fun setWindowBrightness(value: Float) = runOnUiThread {
            window.attributes = window.attributes.apply { screenBrightness = value }
        }
    }

    private fun mimeType(path: String): String = when (path.substringAfterLast('.', "")) {
        "html" -> "text/html"
        "css" -> "text/css"
        "js", "mjs" -> "text/javascript"
        "json" -> "application/json"
        "svg" -> "image/svg+xml"
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "webp" -> "image/webp"
        "ico" -> "image/x-icon"
        "woff2" -> "font/woff2"
        else -> "application/octet-stream"
    }

    private companion object {
        const val EXTRA_URL = "url"
        const val EXTRA_HOME = "home"
        const val EXTRA_PAIR = "pair"
        const val EXTRA_LIVE = "live"
        const val EXTRA_LIVE_KEY = "livekey"
        const val PREF_LIVE = "live"
        const val PREF_LIVE_KEY = "live_key"
        const val LIVE_TIMEOUT_MS = 2_000
        const val LIVE_RETRY_MS = 60_000L
        const val PREF_URL = "url"

        // Domain reserved by Google for apps serving their own assets to a WebView.
        const val ASSET_HOST = "appassets.androidplatform.net"
        const val BUNDLED_URL = "https://$ASSET_HOST/index.html"
        val MICROPHONE_HOSTS = setOf(ASSET_HOST, "127.0.0.1", "localhost")
        val CAPTURE = mapOf(
            PermissionRequest.RESOURCE_AUDIO_CAPTURE to Manifest.permission.RECORD_AUDIO,
            PermissionRequest.RESOURCE_VIDEO_CAPTURE to Manifest.permission.CAMERA,
        )

        const val RETRY_MS = 15_000L
        const val CORNER_DP = 96
        const val CORNER_TAPS = 5
        const val CORNER_WINDOW_MS = 3_000L
    }
}
