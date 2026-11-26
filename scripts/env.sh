# Sourced by the other scripts: locate the JDK, Android SDK and adb.
: "${JAVA_HOME:=/opt/homebrew/opt/openjdk@17}"
: "${ANDROID_HOME:=$HOME/Library/Android/sdk}"
export JAVA_HOME ANDROID_HOME
ADB="${ADB:-$ANDROID_HOME/platform-tools/adb}"
