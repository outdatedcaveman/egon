# Egon Connect — Android app

Tiny (13KB) WebView shell + **Share-sheet target** over the LAN mobile Connect
app (lib/mobile_connect.py, token-guarded :8765). Built 2026-06-11 without
Gradle/Android Studio using the raw toolchain at C:\Users\bruno\egon_android\
(Temurin JDK 17 + cmdline-tools + platform-34 + build-tools 34.0.0).

NOTE: the LAN token is BAKED INTO MainActivity.java at build time. If the
token in egon-config.json (connect_mobile.token) or the PC's LAN IP changes,
rebuild + reinstall.

Build (space-free path required; .bat tools pick up system Java 8 — invoke the
jars with the local JDK directly):
  javac -source 8 -target 8 -bootclasspath sdk/platforms/android-34/android.jar -d out/obj src/.../MainActivity.java
  java -cp sdk/build-tools/34.0.0/lib/d8.jar com.android.tools.r8.D8 --lib android.jar --release --output out/dex <classes>
  aapt2 link -o out/app.unsigned.apk --manifest AndroidManifest.xml -I android.jar
  jar -uf out/app.unsigned.apk -C out/dex classes.dex
  zipalign -f 4 unsigned aligned
  java -jar apksigner.jar sign --ks debug.keystore ...
  adb install -r out/EgonConnect.apk

## v1.1 — floating bubble (2026-06-11)
BubbleService: draggable ✨ chat-head over every app (TYPE_APPLICATION_OVERLAY;
permission pre-granted via `adb shell appops set <pkg> SYSTEM_ALERT_WINDOW
allow`). Tap bubble -> compact WebView Connect panel over the current app;
✕ collapses, ✖ quits. Started by MainActivity on launch.
Build gotchas added: no lambdas with -source 8 against android.jar (use
anonymous classes); feed d8 a classes.jar (shell eats the $1 in inner-class
filenames if passed individually).
