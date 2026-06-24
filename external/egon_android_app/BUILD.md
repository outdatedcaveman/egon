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

## v1.3 — durable bubble + self-healing grants (2026-06-24)
The bubble kept vanishing: a plain background Service is reaped by Android once
the app is cached, and MainActivity is `singleTask` so re-tapping the icon hits
`onNewIntent` (not `onCreate`, the only place the bubble was started). Fixes:
- **BubbleService is now a FOREGROUND service** (`foregroundServiceType=specialUse`
  on API 34 — needs `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_SPECIAL_USE`
  perms and a `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` `<property>` on the `<service>`).
  IMPORTANCE_MIN ongoing notification. Survives backgrounding; verified
  `isForeground=true types=0x40000000` persists after HOME.
- `onStartCommand` returns **START_STICKY**; MainActivity re-starts the service
  in **onResume** so reopening Egon always revives the bubble.
- **Grants self-heal from the PC** now: `egon_app/services/phone_keepalive_service.py`
  (running always-on inside egon_core) re-asserts the a11y grant + SYSTEM_ALERT_WINDOW
  / ACCESS_RESTRICTED_SETTINGS appops whenever the phone is connected, so an
  `install`/reinstall that wipes them is repaired within one poll. a11y is turned
  OFF in banking mode; the overlay appop is kept on (it doesn't block banking).

### Build it
A runnable `build.sh` now sits beside this file (was prose-only): from the raw
toolchain root `C:\Users\bruno\egon_android` run `bash build.sh` →
`out/EgonConnect.apk`, then `adb -s <target> install -r out/EgonConnect.apk`.
