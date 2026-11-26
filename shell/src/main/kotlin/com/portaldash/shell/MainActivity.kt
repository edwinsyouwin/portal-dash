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
import java.io.IOException

/**
 * Full-screen WebView that shows the dashboard.
 *
 * By default it serves the files bundled in the APK. A different URL can be set with
 *   adb shell am start -n com.portaldash.shell/.MainActivity --es url http://host:port/
 * and cleared again with `--es url default`. The choice is remembered across restarts.
 */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private lateinit var prefs: SharedPreferences

    private val handler = Handler(Looper.getMainLooper())
    private val retryLoad = Runnable { webView.loadUrl(startUrl()) }

    private val bleRelay by lazy { BleRelay(this) }

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
        webView.loadUrl(startUrl())
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        applyHomeExtra(intent)
        applyPairExtra(intent)
        if (applyUrlExtra(intent)) {
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

    /** Lets the bundled dashboard (and the dev server) use the microphone, and nothing else. */
    private inner class MicrophoneClient : WebChromeClient() {
        override fun onPermissionRequest(request: PermissionRequest) {
            val trusted = request.origin.host in MICROPHONE_HOSTS
            val wantsMicrophone = PermissionRequest.RESOURCE_AUDIO_CAPTURE in request.resources
            val granted = checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
            if (trusted && wantsMicrophone && granted) {
                request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            } else {
                // scripts/install.sh grants RECORD_AUDIO; ask in case it was installed another way.
                if (trusted && wantsMicrophone) {
                    requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 0)
                }
                request.deny()
            }
        }
    }

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
        const val PREF_URL = "url"

        // Domain reserved by Google for apps serving their own assets to a WebView.
        const val ASSET_HOST = "appassets.androidplatform.net"
        const val BUNDLED_URL = "https://$ASSET_HOST/index.html"
        val MICROPHONE_HOSTS = setOf(ASSET_HOST, "127.0.0.1", "localhost")

        const val RETRY_MS = 15_000L
        const val CORNER_DP = 96
        const val CORNER_TAPS = 5
        const val CORNER_WINDOW_MS = 3_000L
    }
}
