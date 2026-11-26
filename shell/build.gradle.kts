plugins {
    id("com.android.application") version "9.4.1"
}

android {
    namespace = "com.portaldash.shell"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.portaldash.shell"
        // Portal devices run Android 9 (API 28) or 10 (API 29).
        minSdk = 28
        targetSdk = 36
        versionCode = 1
        versionName = "0.1"
    }

    // The dashboard is plain static files; bundle them as-is so the app works offline.
    sourceSets {
        getByName("main") {
            assets.srcDir("../dashboard")
        }
    }
}
