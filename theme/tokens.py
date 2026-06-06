"""Streamlit-aesthetic design tokens — light + dark schemes via CSS variables.

Light theme = the original Streamlit palette.
Dark  theme = the same hierarchy but inverted; same red/amber accents work on both.
Toggle by setting `class="dark"` on <html> (we persist via egon-config.json).
"""

# Legacy named exports (kept for any direct callers)
ACCENT = "#ff4b4b"
LEDGER = "#f59e0b"

FONT = "'Source Sans Pro', 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif"

GLOBAL_CSS = """
<style>
  :root {
    --bg:           #ffffff;
    --surface:      #f8f9fb;
    --surface-2:    #f0f2f6;
    --border:       #e1e4e8;
    --border-soft:  #f0f2f6;
    --text:         #262730;
    --text-2:       #374151;
    --muted:        #6b7280;
    --muted-soft:   #9ca3af;

    --accent:       #ff4b4b;
    --accent-bg:    #ff4b4b22;
    --accent-txt:   #b3261e;

    --ledger:       #f59e0b;
    --ledger-bg:    #f59e0b22;
    --ledger-txt:   #b45309;
    --ledger-dark:  #92400e;
    --ledger-txt-soft: #a16207;

    --success:      #16a34a;
    --danger:       #dc2626;
    --warn-bg:      #fffbeb;
    --warn-border:  #fde68a;

    --hero-grad-1:  #fef3c7;
    --hero-grad-2:  #fde68a;
    --hero-border:  #fbbf24;

    --chip-info-bg: #e0f2fe;
    --chip-info-fg: #075985;
    --chip-sug-bg:  #dcfce7;
    --chip-sug-fg:  #166534;
    --chip-warn-bg: #fef3c7;
    --chip-warn-fg: #92400e;

    --code-bg:      #f8f9fb;
    --code-fg:      #374151;

    --ledger-stack-1: #fde68a;
    --ledger-stack-2: #f59e0b;
    --ledger-stack-3: #475569;
    --ledger-stack-4: #262730;
  }

  html.dark {
    --bg:           #0e1117;
    --surface:      #161a23;
    --surface-2:    #1c2230;
    --border:       #1f242e;
    --border-soft:  #1a1f29;
    --text:         #e6e7ea;
    --text-2:       #cfd2da;
    --muted:        #9aa0ac;
    --muted-soft:   #7c8290;

    --accent:       #ef4444;
    --accent-bg:    #ef444433;
    --accent-txt:   #fca5a5;

    --ledger:       #f59e0b;
    --ledger-bg:    #f59e0b33;
    --ledger-txt:   #fbbf24;
    --ledger-dark:  #fde68a;
    --ledger-txt-soft: #fbbf24;

    --success:      #4ade80;
    --danger:       #f87171;
    --warn-bg:      #1c1610;
    --warn-border:  #78350f;

    --hero-grad-1:  #1c1610;
    --hero-grad-2:  #2a1c08;
    --hero-border:  #b45309;

    --chip-info-bg: #1e3a8a33;
    --chip-info-fg: #93c5fd;
    --chip-sug-bg:  #14532d33;
    --chip-sug-fg:  #86efac;
    --chip-warn-bg: #78350f33;
    --chip-warn-fg: #fcd34d;

    --code-bg:      #11141b;
    --code-fg:      #cfd2da;

    --ledger-stack-1: #fbbf2466;
    --ledger-stack-2: #f59e0b;
    --ledger-stack-3: #94a3b8;
    --ledger-stack-4: #e6e7ea;
  }

  body, .q-page, .nicegui-content {
    background: var(--bg) !important;
    color: var(--text) !important;
    font-family: """ + FONT + """ !important;
  }
  .q-drawer { background: var(--surface-2) !important; border-right: 1px solid var(--border) !important; }
  .q-header { background: var(--bg) !important; border-bottom: 1px solid var(--border) !important;
              box-shadow: none !important; color: var(--text) !important; }
  .q-toolbar__title { color: var(--text); }
  .q-card { box-shadow: none !important; border: 1px solid var(--border); border-radius: 6px;
            background: var(--bg); color: var(--text); }
  .q-btn--unelevated { box-shadow: none !important; }
  .q-page { padding: 0 !important; }
  code { background: var(--code-bg); color: var(--code-fg); padding: 1px 5px; border-radius: 3px;
         font-size: 0.92em; }

  .nav-item {
    display: flex; align-items: center; gap: 10px;
    padding: 7px 12px; margin: 2px 8px; border-radius: 6px;
    color: var(--text-2); font-size: 14px; cursor: pointer; text-decoration: none;
  }
  .nav-item:hover { background: var(--border); }
  .nav-item.sel { background: var(--accent-bg); color: var(--accent-txt); font-weight: 600; }
  .nav-item.sel.ledger { background: var(--ledger-bg); color: var(--ledger-txt); }

  .kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
         padding: 14px 16px; }
  .kpi:hover { background: var(--surface-2); }
  .kpi.hero {
    background: linear-gradient(135deg, var(--hero-grad-1) 0%, var(--hero-grad-2) 100%);
    border-color: var(--hero-border);
  }
  .kpi .lbl { font-size: 13px; color: var(--muted); margin-bottom: 4px; }
  .kpi.hero .lbl { color: var(--ledger-dark); font-weight: 600; }
  .kpi .val { font-size: 28px; font-weight: 700; color: var(--text); line-height: 1.1;
              font-variant-numeric: tabular-nums; }
  .kpi.hero .val { font-size: 32px; color: var(--ledger-dark); }
  .kpi .delta.up { color: var(--danger); font-size: 12px; margin-top: 4px; }
  .kpi .delta.dn { color: var(--success); font-size: 12px; margin-top: 4px; }
  .kpi .delta.flat { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .kpi .sub { font-size: 12px; color: var(--muted); margin-top: 4px; }

  .panel { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .panel .phead { padding: 12px 18px; background: var(--surface);
                  border-bottom: 1px solid var(--border);
                  display: flex; justify-content: space-between; align-items: center; }
  .panel .phead .ttl { font-size: 14px; font-weight: 600; color: var(--text); }
  .panel .phead .lnk { font-size: 12px; color: var(--ledger-txt); cursor: pointer; }
  .panel .pbody { padding: 16px 18px; color: var(--text-2); }
  .panel .pbody.flush { padding: 0; }

  .stbl { width: 100%; border-collapse: collapse; font-size: 13px; }
  .stbl th { text-align: left; padding: 8px 18px; color: var(--muted); font-weight: 600;
             border-bottom: 1px solid var(--border); font-size: 12px; background: var(--surface); }
  .stbl td { padding: 10px 18px; border-bottom: 1px solid var(--border-soft); color: var(--text-2);
             font-variant-numeric: tabular-nums; }
  .stbl td.r { text-align: right; }
  .stbl tr:last-child td { border-bottom: none; }
  .stbl tr:hover td { background: var(--surface); }
  .stbl .num { color: var(--text); font-weight: 500; }
  .stbl .cost { color: var(--ledger-txt); font-weight: 600; }

  .chip { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px;
          font-weight: 500; background: var(--chip-info-bg); color: var(--chip-info-fg); }
  .chip.sug  { background: var(--chip-sug-bg); color: var(--chip-sug-fg); }
  .chip.warn { background: var(--chip-warn-bg); color: var(--chip-warn-fg); }
  .chip.opus   { background: #ede9fe; color: #6d28d9; }
  .chip.sonnet { background: #dbeafe; color: #1d4ed8; }
  .chip.haiku  { background: #dcfce7; color: #166534; }
  html.dark .chip.opus   { background: #5b21b633; color: #c4b5fd; }
  html.dark .chip.sonnet { background: #1e3a8a33; color: #93c5fd; }
  html.dark .chip.haiku  { background: #14532d33; color: #86efac; }

  .bar-row { display: flex; align-items: center; gap: 10px; padding: 7px 0; font-size: 13px; }
  .bar-row .lbl { width: 110px; color: var(--text-2); flex-shrink: 0; }
  .bar-row .bar { flex: 1; height: 16px; background: var(--surface-2); border-radius: 3px; overflow: hidden; }
  .bar-row .bar i { display: block; height: 100%; border-radius: 3px; }
  .bar-row .v { width: 90px; text-align: right; color: var(--muted); font-size: 12px; flex-shrink: 0;
                font-variant-numeric: tabular-nums; }
  .bar-row .v b { color: var(--text); font-weight: 600; }

  .flag { background: var(--warn-bg); border: 1px solid var(--warn-border); border-radius: 6px;
          padding: 12px 14px; margin-bottom: 16px; font-size: 13px; color: var(--ledger-dark);
          display: flex; gap: 10px; align-items: flex-start; }
  .flag b { color: var(--ledger-dark); }

  .status-pill { display: inline-flex; align-items: center; gap: 6px; background: var(--surface);
                 border: 1px solid var(--border); padding: 5px 12px; border-radius: 999px;
                 font-size: 12px; color: var(--text-2); margin-right: 8px; margin-bottom: 8px; }
  .status-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); }
  .status-pill .dot.warn { background: var(--ledger); }

  h1.page { font-size: 30px; font-weight: 700; margin: 0 0 4px; color: var(--text);
            letter-spacing: -0.3px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  h1.page .badge { font-size: 10px; padding: 3px 9px; border-radius: 12px;
                   background: var(--chip-warn-bg); color: var(--ledger-txt);
                   font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase; }
  p.page-sub { color: var(--muted); margin: 0 0 18px; font-size: 14px; }
  p.page-sub code { background: var(--code-bg); }

  .digest-bullet { padding: 10px 18px; border-bottom: 1px solid var(--border-soft);
                   font-size: 13px; display: flex; gap: 10px; color: var(--text-2); }
  .digest-bullet:last-child { border-bottom: none; }
  .digest-bullet .dot { width: 5px; height: 5px; border-radius: 50%; background: var(--accent);
                        margin-top: 7px; flex-shrink: 0; }

  .skill-row { display: flex; align-items: center; gap: 10px; padding: 9px 18px;
               border-bottom: 1px solid var(--border-soft); font-size: 13px; }
  .skill-row:last-child { border-bottom: none; }
  .skill-row .name { flex: 1; color: var(--text-2); }
  .skill-row .name b { color: var(--text); font-weight: 600; }
  .skill-row .name small { font-size: 11px; color: var(--muted); }
  .skill-row .ct { color: var(--muted); font-size: 12px; margin-right: 12px; }
  .skill-row .cost { color: var(--ledger-txt); min-width: 60px; text-align: right; font-weight: 600;
                     font-variant-numeric: tabular-nums; }

  /* pre / log tail */
  pre { background: var(--code-bg) !important; color: var(--code-fg) !important; }

  /* ---- Quasar input theming for dark mode (and tighter contrast in light) ---- */
  input, textarea, .q-field__native {
    color: var(--text) !important;
  }
  input::placeholder, textarea::placeholder, .q-field__native::placeholder {
    color: var(--muted) !important;
    opacity: 0.85 !important;
  }
  /* outlined input border */
  .q-field--outlined .q-field__control {
    background: var(--surface) !important;
    color: var(--text) !important;
  }
  .q-field--outlined .q-field__control:before {
    border-color: var(--border) !important;
  }
  .q-field--outlined .q-field__control:hover:before {
    border-color: var(--muted) !important;
  }
  .q-field--outlined.q-field--focused .q-field__control:after {
    border-color: var(--accent) !important;
  }
  .q-field__label {
    color: var(--muted) !important;
  }
  .q-field--focused .q-field__label {
    color: var(--accent) !important;
  }

  /* Eye-toggle icon for password fields — was nearly invisible in dark mode */
  .q-icon {
    color: var(--muted) !important;
  }
  .q-icon:hover {
    color: var(--text) !important;
  }

  /* toggle (plan mode) — Quasar default invisible on dark bg */
  .q-toggle__inner {
    color: var(--muted) !important;
  }
  .q-btn-toggle .q-btn {
    color: var(--text-2) !important;
  }
  .q-btn-toggle .q-btn.q-btn--active {
    background: var(--accent-bg) !important;
    color: var(--accent-txt) !important;
  }

  /* Buttons — Quasar's default flat buttons on dark = invisible. Force-color. */
  .q-btn--outline {
    color: var(--text-2) !important;
  }
  .q-btn--outline:hover {
    background: var(--surface) !important;
  }

  /* Tabs (used in Memory + Artifacts windows) */
  .q-tab {
    color: var(--text-2) !important;
  }
  .q-tab--active {
    color: var(--accent) !important;
  }
  .q-tabs__content {
    color: var(--text-2);
  }
  .q-tab__indicator {
    background: var(--accent) !important;
  }

  /* Notify pop-ups: keep them readable on either theme */
  .q-notification {
    color: var(--text);
  }

  /* Select dropdowns / menus — Quasar default white-on-white in dark */
  .q-menu, .q-list, .q-virtual-scroll__content {
    background: var(--surface) !important;
    color: var(--text) !important;
    border: 1px solid var(--border);
  }
  .q-item {
    color: var(--text-2) !important;
  }
  .q-item:hover, .q-item--active, .q-item.q-router-link--active {
    background: var(--surface-2) !important;
    color: var(--accent-txt) !important;
  }

  /* Cards in dark mode: lift slightly off background */
  html.dark .q-card {
    background: var(--surface) !important;
  }
  html.dark .panel {
    background: var(--surface) !important;
  }

  /* "stub" chip (and generic chips without specific class) — boost contrast */
  .chip:not(.sug):not(.warn):not(.opus):not(.sonnet):not(.haiku) {
    background: #1e3a8a33 !important;
    color: #93c5fd !important;
  }
  html.dark .chip:not(.sug):not(.warn):not(.opus):not(.sonnet):not(.haiku) {
    background: #1e3a8a44 !important;
    color: #cfe2ff !important;
  }

  /* Upload widget — NiceGUI's default uses opacity:0.5 on the file list which
     makes uploaded filenames nearly invisible in dark mode. Force-readable. */
  .q-uploader {
    background: var(--surface) !important;
    color: var(--text) !important;
    border: 1px solid var(--border) !important;
  }
  .q-uploader__header {
    background: var(--surface-2) !important;
    color: var(--text) !important;
  }
  .q-uploader__title, .q-uploader__subtitle,
  .q-uploader__file, .q-uploader__file-header, .q-uploader__file-title,
  .q-uploader__file-status, .q-uploader__file-meta {
    color: var(--text) !important;
    opacity: 1 !important;
  }
  .q-uploader__file--idle .q-uploader__file-status,
  .q-uploader__file--uploaded {
    opacity: 1 !important;
    color: var(--success) !important;
  }
  .q-uploader__file--uploaded .q-uploader__file-title {
    color: var(--text) !important;
  }
  /* "uploaded" check icon */
  .q-uploader__file .q-icon { color: var(--success) !important; }

  /* The dropdown badge "no file" in dark mode was orange-on-white */
  .q-chip {
    color: var(--text) !important;
  }

  /* Hover state for nav-item when selected — keep readable */
  .nav-item.sel:hover { background: var(--accent-bg); }
  .nav-item.sel.ledger:hover { background: var(--ledger-bg); }

  /* Make sure long page content can scroll without overlapping the drawer */
  main { min-height: calc(100vh - 56px); }
</style>
"""
