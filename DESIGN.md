# Design System: Egon — Personal Control Plane

**Format:** Google Labs' [DESIGN.md spec](https://github.com/google-labs-code/stitch-skills/tree/main/skills/design-md) (Apache-2.0, alpha · April 2026).
**Purpose:** Every AI agent or developer touching Egon must read this first. It's the brand contract — colors, typography, components, spacing — so the UI stays coherent across sessions.

---

## 1. Visual Theme & Atmosphere

Egon is a **dense, data-honest control plane** for a personal KMS. The aesthetic is **Streamlit-inspired functional minimalism** — a working tool, not a portfolio piece. Every pixel must earn its keep. The mood is **calm density**: lots of information at a glance, but never noisy.

Two themes coexist:
- **Light theme** — clean white surfaces, hairline borders, generous breathing room. Default for daytime work.
- **Dark theme** — near-black canvas with subtle surface lift, identical hierarchy. Default for late-night sessions.

Both themes share the same red-and-amber accent palette so muscle memory transfers; only the neutrals invert.

**Key characteristics:**
- **Information-first** — tables, KPI cards, status pills are the building blocks. Decorative graphics are rare.
- **Skeleton-then-stream** — every page renders structure in <500ms, data fills in lazily. No frozen waits.
- **Iconography over text booleans** — `♥`/`♡` for liked, `★`/`☆` for starred, `Z✓`/`B✓` for synced. Never raw `True`/`False`.
- **Hairline borders, no shadows** in light mode. Subtle elevation in dark mode via surface lifts, not box-shadow.
- **Always-on dark-mode toggle** in the header.

---

## 2. Color Palette & Roles

All colors live as CSS variables in `theme/tokens.py`. Use the variable name (`var(--accent)`), not the raw hex, in component code.

### Light theme (default)

| Role | Variable | Hex | Description |
|---|---|---|---|
| Page background | `--bg` | `#ffffff` | Crisp paper-white canvas |
| Card / panel surface | `--surface` | `#f8f9fb` | Whisper-cool light gray for KPI cards |
| Surface lift | `--surface-2` | `#f0f2f6` | Slightly cooler, for hover and section headers |
| Border | `--border` | `#e1e4e8` | Hairline gray for panel + table dividers |
| Soft border | `--border-soft` | `#f0f2f6` | Inside-table row separators |
| Primary text | `--text` | `#262730` | Deep charcoal, the workhorse |
| Secondary text | `--text-2` | `#374151` | Slate for body copy |
| Muted text | `--muted` | `#6b7280` | Cool gray for metadata, captions, placeholders |
| Faintest text | `--muted-soft` | `#9ca3af` | "—" placeholders and disabled hints |

### Dark theme (invert)

| Role | Variable | Hex | Description |
|---|---|---|---|
| Page background | `--bg` | `#0e1117` | Deep slate-black, never pure black |
| Card / panel surface | `--surface` | `#161a23` | Lifted card surface for visual separation |
| Surface lift | `--surface-2` | `#1c2230` | Hover + section headers |
| Border | `--border` | `#1f242e` | Subtle slate hairline |
| Soft border | `--border-soft` | `#1a1f29` | Row separators |
| Primary text | `--text` | `#e6e7ea` | Off-white for body |
| Secondary text | `--text-2` | `#cfd2da` | Slightly dimmed for hierarchy |
| Muted text | `--muted` | `#9aa0ac` | Captions |
| Faintest text | `--muted-soft` | `#7c8290` | Faded placeholders |

### Semantic accents (shared across themes)

| Role | Variable | Hex (light) | Hex (dark) | Use |
|---|---|---|---|---|
| Streamlit Red | `--accent` | `#ff4b4b` | `#ef4444` | Primary action buttons, selected nav item, focused input border, active tab indicator |
| Streamlit Red bg | `--accent-bg` | `#ff4b4b22` | `#ef444433` | Selected nav background tint |
| Streamlit Red text | `--accent-txt` | `#b3261e` | `#fca5a5` | Selected nav text |
| Token-ledger Amber | `--ledger` | `#f59e0b` | `#f59e0b` | Money / cost / token-ledger semantics, stars, "ledger" sub-navigation |
| Ledger bg / text | `--ledger-bg` / `--ledger-txt` | `#f59e0b22` / `#b45309` | `#f59e0b33` / `#fbbf24` | Ledger-themed chips + selected sub-nav |
| Success | `--success` | `#16a34a` | `#4ade80` | "ok" chips, ✓ checks, hit-rate ≥ 90% |
| Danger | `--danger` | `#dc2626` | `#f87171` | Error chips, ▲ over-budget deltas |
| Hero gradient | `--hero-grad-1` / `--hero-grad-2` | `#fef3c7` → `#fde68a` | `#1c1610` → `#2a1c08` | Hero KPI cards (e.g. "Tokens today") |

**Color rule of thumb:** use **descriptive variable names** (`var(--accent)`) — never `#ff4b4b` directly in component code. This is what makes theme-switching work.

---

## 3. Typography Rules

**Primary font family:** **Source Sans Pro**, with `'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif` as fallback. Set globally via `--font` variable.

**Character:** Humanist sans-serif with technical clarity. Tighter than typical body fonts so dense data tables stay readable.

### Hierarchy

| Element | Class | Size | Weight | Notes |
|---|---|---|---|---|
| Page title | `h1.page` | 30px | 700 | Letter-spacing `-0.3px`. Flex row so a status chip + badge can sit beside it. |
| Section header | inline `<h2>` | 18px | 600 | Color `var(--text)` |
| KPI value | `.kpi .val` | 28px | 700 | `font-variant-numeric: tabular-nums` for clean alignment |
| Hero KPI value | `.kpi.hero .val` | 32px | 700 | Amber `var(--ledger-dark)` for token-ledger context |
| KPI label | `.kpi .lbl` | 13px | regular | `var(--muted)` |
| KPI sub-text | `.kpi .sub` | 12px | regular | `var(--muted)` |
| Body text | `p.page-sub` | 14px | regular | `var(--muted)` |
| Table cell | `.stbl td` | 13px | regular | `font-variant-numeric: tabular-nums` for numeric columns |
| Table header | `.stbl th` | 12px | 600 | `var(--muted)`, light surface background |
| Status pill text | `.status-pill` | 12px | regular | `var(--text-2)` |
| Chip text | `.chip` | 11px | 500 | Color depends on `.chip.sug` / `.chip.warn` modifiers |
| Caption / metadata | inline | 11px | regular | `var(--muted)` |

### Spacing principles
- Page titles: 0 margin-top, 4px margin-bottom (status-pill row follows tight)
- Section headers: 24px above, 10px below
- KPI grid: 14px gap between cards, 18-24px below the grid
- Tables: 8-10px row padding, never collapsed (rows must breathe)
- Form inputs: 6px between consecutive inputs in a column

---

## 4. Component Stylings

### KPI cards (`.kpi`)
- **Background:** `var(--surface)` light, `var(--surface)` dark (lifted)
- **Border:** 1px `var(--border)`, **no box-shadow**
- **Corner radius:** 6px — modern but unfussy
- **Padding:** 14px vertical, 16px horizontal
- **Hover:** background tints to `var(--surface-2)`. No transform, no shadow.
- **Hero variant** (`.kpi.hero`): amber gradient background (`linear-gradient(135deg, var(--hero-grad-1), var(--hero-grad-2))`), 32px value, used for the headline of a metric strip (e.g. "Today's spend")

### Panels (`.panel`)
- **Background:** `var(--bg)` light, `var(--surface)` dark
- **Border:** 1px `var(--border)`
- **Corner radius:** 6px
- **Header bar** (`.phead`): 12px×18px padding, `var(--surface)` light background, bottom border, flex row with title left + soft link right
- **Body** (`.pbody`): 16px×18px padding. Use `.pbody.flush` (zero padding) when a table fills the panel.

### Buttons
- **Primary** (`unelevated`): `background: var(--accent)`, `color: white`, 7px×16px padding, 6px radius. On hover, no transform — just a small background-darken via Quasar's default.
- **Secondary** (`unelevated outline`): `color: var(--text-2)`, 1px border `var(--border)`. Hover background: `var(--surface)`.
- **No drop shadows** on any button. NiceGUI's `unelevated` flag is mandatory.
- **Tooltip** for any icon-only or terse button (`elem.tooltip("…")`) — never assume users know what an icon means.

### Chips (`.chip`)
- **Base:** 11px text, 1px×8px padding, 10px radius, pill shape
- **`.chip.sug`** — success bg `#dcfce7` / text `#166534` (light); `#14532d33` / `#86efac` (dark). For "ok", "running", "authorized".
- **`.chip.warn`** — warning bg `#fef3c7` / text `#92400e` (light); `#78350f33` / `#fcd34d` (dark). For "unconfigured", "offline".
- **Default** (no modifier) — info blue (`#1e3a8a33` / `#cfe2ff` in dark) for generic labels
- **Model chips** (`.chip.opus` / `.chip.sonnet` / `.chip.haiku`) — purple / blue / green pairings for ledger context

### Status pills (`.status-pill`)
- A chip-shaped container that wraps short fact-pairs: `<label> <bold-value>`
- 5px×12px padding, 999px radius, hairline border
- Used at the top of pages for at-a-glance state (Phone status, Sweep state, History count, etc.)

### Tables (`.stbl`)
- **Header:** `var(--surface)` background, 12px text, semi-bold, uppercase tolerated for super-dense data only
- **Rows:** 9-10px padding, hairline bottom border (`var(--border-soft)`), hover surface tint
- **Numeric columns:** tabular-nums + right-aligned via `.r` class
- **Currency / cost:** `var(--ledger-txt)` color, 600 weight
- **Truncation:** title cells max 80-120 chars then ellipsis; URL row gets a smaller secondary line in `var(--muted)`

### Inputs (Quasar `outlined dense stack-label`)
- **Always use `stack-label`** so the label sits above, never overlapping placeholder
- **Border:** `var(--border)` resting, `var(--accent)` focused
- **Padding:** 10px-12px
- **Password fields:** `password=True, password_toggle_button=True` — never hidden behind weird UX
- **Help text under input:** 11px, `var(--muted)`

### Skeleton placeholders (lazy-load)
- **`@keyframes pulse`** between 0.55 and 0.95 opacity, 1.5s ease-in-out infinite
- **Skeleton bars:** `height:14px; background: var(--surface-2); border-radius: 4px; margin: 8px 0`
- **Chip placeholder:** same `.chip` shape with `background: var(--surface-2)`, `color: transparent`, 60% opacity, pulse animation
- Show skeleton FIRST, real data streams in via `ui.timer(0.05, async_loader, once=True)`. See `views/_async.py`.

### Media grid cards (`views/_cards.py::render_grid`)
- **Aspect ratio:** 2:3 (poster format)
- **Background:** image if available; otherwise a hash-derived `hsl()` gradient with the title overlaid centered
- **Overlay badges:** year (top-left, black-translucent), liked-heart (top-right)
- **Below poster:** half-star rating row, then 2-line truncated title (12px, `var(--text)`)
- **Hover:** `translateY(-2px)` and a soft drop-shadow appears (one of the few places shadows are allowed)

### Navigation drawer
- **Width:** 244px fixed
- **Background:** `var(--surface-2)`
- **Item:** 7px×12px padding, 6px radius, gap 10px between icon + label
- **Selected:** `var(--accent-bg)` background, `var(--accent-txt)` text, 600 weight. Ledger nav item gets the amber variant.
- **Footer:** flex-pinned at bottom with version + state path, 11px, `var(--muted-soft)`

---

## 5. Layout Principles

### Grid & structure
- **Page width:** content max-width 1380px; Navigation page is full-bleed (no max) because it renders a real-time control panel
- **Three-section page rhythm:** `<h1.page>` → status-pill row → content panels (one or many)
- **KPI grid:** typically `grid-template-columns: 1.3fr 1fr 1fr 1fr 1fr` (hero card slightly wider)
- **Two-column row pattern:** `grid-template-columns: 2fr 1fr` (chart + side panel) or `1fr 1fr` (Categories + Settings)

### Whitespace
- **Page padding:** 28px vertical, 40px horizontal
- **Between sections:** 14-18px gap
- **Inside panels:** 16-18px padding
- **Page subtitle to first panel:** 18-24px gap

### Responsive
- **Single-developer machine** is the primary form factor (laptop or desktop). No mobile design required for v1.
- The pywebview window can be resized; layouts flex naturally because of grid + flex.
- Below ~1100px the drawer should collapse (not yet implemented — flag for later).

---

## 6. Notes for AI Agents Generating Egon UI

When generating new screens or extending existing ones, use this language:

### Vocabulary
- **"Streamlit-aesthetic"** — light bg, hairline borders, Source Sans Pro, red accent. Never "Material" or "Tailwind defaults".
- **"Themed via CSS variables"** — every color reference is `var(--name)`, never raw hex. This is what makes light/dark switching work.
- **"Skeleton-then-stream"** — render structure synchronously, defer data to `ui.timer(0.05, async_loader, once=True)`.
- **"Iconography over booleans"** — `♥/♡`, `★/☆`, `Z✓/Z·` — never `True`/`False` text in tables.

### Hard rules
- **Always** wrap `<input>` with `stack-label` prop (no overlap with placeholders).
- **Always** use `ui.element('iframe')` (NEVER `ui.html('<iframe>')`) — NiceGUI's sanitizer strips iframe tags.
- **Never** make a synchronous network call on the render path. Wrap heavy I/O in `lazy_panel(load_fn, render_fn)` from `views/_async.py`.
- **Never** put `box-shadow` on buttons or cards (except on hover for grid cards).
- **Always** add a tooltip to icon-only buttons.

### Component prompts
- *"Add a KPI card with hero variant, label 'Tokens today', value `_fmt_tokens(today_tok)`, and sub-text showing the API counterfactual."*
- *"Wrap this slow API call in `lazy_panel(load_fn=fetch_data, render_fn=render_cards, skeleton=skeleton_panel('Loading X', lines=5))`."*
- *"Use `<span class='status-pill'>label <b>value</b></span>` instead of a regular div for inline facts."*

### Incremental iteration
When refining existing screens:
1. Focus on ONE component at a time.
2. Reference this DESIGN.md by name in commit messages or PR descriptions.
3. If a change requires a new color, add it to `theme/tokens.py` as a CSS variable AND document it here under section 2.
