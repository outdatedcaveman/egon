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

## v1.2 — bubble reads the whole screen (2026-06-11)
The bubble now CAPTURES the current screen's text on tap and auto-searches —
no copy/paste. Implemented with an **AccessibilityService** (EgonA11yService):
reads the foreground app's text node tree (text + contentDescription), passes
it to the mobile page as `&shared=` which auto-runs /m/connect. Text only — no
screenshot, no OCR; nothing leaves the LAN.

Enable over adb (NO user prompt needed), order matters on Android 13+:
```
# 1. lift the "Restricted settings" block that silently reverts a11y grants
#    for sideloaded apps (else `settings put` reads back null):
adb shell appops set org.brunosaramago.egonconnect ACCESS_RESTRICTED_SETTINGS allow
# 2. enable the service (append to existing list with ':' — don't clobber):
adb shell settings put secure enabled_accessibility_services \
  org.brunosaramago.egonconnect/org.brunosaramago.egonconnect.EgonA11yService
adb shell settings put secure accessibility_enabled 1
```
Gotchas:
- `getRootInActiveWindow()` returns the accessibility/input-focused window,
  which is often a STALE background app (first test captured a backgrounded
  WhatsApp voice button instead of Chrome). Fixed by enumerating `getWindows()`
  and taking the focused TYPE_APPLICATION window that isn't our own package,
  falling back to the largest. Requires `flagRetrieveInteractiveWindows` in the
  service config (res/xml/egon_a11y.xml).
- Build now compiles resources: `aapt2 compile --dir app/res -o out/res.zip`
  then pass `out/res.zip` to `aapt2 link` (needed for @xml/egon_a11y +
  @string). No R.java required (Java doesn't reference resources).
- A11y enablement survives `install -r`; SYSTEM_ALERT_WINDOW + restricted-
  settings appops also persist, but reassert them after install to be safe.
