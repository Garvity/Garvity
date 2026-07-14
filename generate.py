#!/usr/bin/env python3
"""
generate.py
------------------------------------------------------------------
Builds an animated "terminal / ASCII" developer profile card as SVG,
meant to be embedded in a GitHub profile README.

Produces TWO files (GitHub's <picture> tag then handles light/dark
mode reliably, which a single SVG with prefers-color-scheme does not
do consistently once GitHub's camo proxy re-hosts the image):

   dist/profile-card-dark.svg
   dist/profile-card-light.svg

Run locally:
   python generate.py

Run in CI (see .github/workflows/update-card.yml):
   GITHUB_TOKEN=xxxx python generate.py

All personal info lives in config.json. Nothing here needs editing
except the THEME palettes if you want a different look.
------------------------------------------------------------------
"""

import json
import math
import os
import sys
from datetime import date, datetime

try:
    import requests
except ImportError:
    requests = None

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
    config = json.load(f)

# Each theme gets its own ASCII art: the light file inverts the brightness
# ramp so the portrait reads correctly on a white background.
ASCII_ART = {}
for _theme in ("dark", "light"):
    with open(os.path.join(HERE, f"ascii_{_theme}.txt"), "r", encoding="utf-8") as f:
        ASCII_ART[_theme] = f.read()


# ---------------------------------------------------------------------------
# 1. AGE / "UPTIME" — computed fresh every time this script runs
# ---------------------------------------------------------------------------
def duration_parts(start_dt, now=None):
    """y/m/d/totalDays between start_dt and now — reused for both DOB age
    and GitHub account age."""
    now = now or datetime.now()

    y = now.year - start_dt.year
    m = now.month - start_dt.month
    d = now.day - start_dt.day

    if d < 0:
        m -= 1
        # last day of previous month relative to `now`
        if now.month == 1:
            prev_month_days = (date(now.year - 1, 12, 31)).day
        else:
            prev_month_days = (date(now.year, now.month, 1) - date(now.year, now.month - 1, 1)).days
        d += prev_month_days
    if m < 0:
        y -= 1
        m += 12

    total_days = (now - start_dt).days
    return {"y": y, "m": m, "d": d, "totalDays": total_days}


def account_age_fields(created_iso, has_time=True):
    """Given an ISO date (GitHub createdAt, e.g. 2019-06-01T12:00:00Z) or a
    plain YYYY-MM-DD fallback date, return formatted account-age fields."""
    if has_time:
        created_dt = datetime.strptime(created_iso, "%Y-%m-%dT%H:%M:%SZ")
    else:
        created_dt = datetime.strptime(created_iso, "%Y-%m-%d")
    parts = duration_parts(created_dt)
    return {
        "accountCreated": created_dt.strftime("%b %Y"),
        "accountAge": f'{parts["y"]}y {parts["m"]}m',
        "accountAgeDays": f'{parts["totalDays"]:,} days',
    }


age = duration_parts(datetime.strptime(config["birthDate"], "%Y-%m-%d"))
uptime_string = f'{age["y"]}y {age["m"]}m {age["d"]}d'


# ---------------------------------------------------------------------------
# 2. GITHUB STATS — real numbers if GITHUB_TOKEN is present, else fallback
#    dummy numbers from config.json so the card always renders something.
# ---------------------------------------------------------------------------
def fetch_github_stats(username):
    token = os.environ.get("GITHUB_TOKEN")
    fallback = config["statsFallback"]

    def with_fallback_account_age(result):
        # Account age is still *calculated*, just from a fallback creation
        # date (statsFallback.accountCreated in config.json) since we have
        # no API access to ask GitHub for the real one.
        fallback_created = fallback.get("accountCreated")
        if fallback_created:
            result = {**result, **account_age_fields(fallback_created, has_time=False)}
        return result

    if not token or not username:
        print("[generate.py] No GITHUB_TOKEN/githubUsername set — using statsFallback dummy numbers.")
        return with_fallback_account_age({**fallback, "source": "fallback"})

    if requests is None:
        print("[generate.py] 'requests' package not installed — using statsFallback dummy numbers.")
        return with_fallback_account_age({**fallback, "source": "fallback"})

    gql_headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        query = """
      query($login: String!) {
        user(login: $login) {
          createdAt
          followers { totalCount }
          following { totalCount }
          repositories(first: 100, ownerAffiliations: OWNER, isFork: false) {
            totalCount
            nodes { stargazerCount }
          }
          contributionsCollection {
            contributionCalendar { totalContributions }
            totalCommitContributions
            restrictedContributionsCount
          }
        }
      }"""

        res = requests.post(
            "https://api.github.com/graphql",
            headers=gql_headers,
            json={"query": query, "variables": {"login": username}},
            timeout=30,
        )
        payload = res.json()
        if payload.get("errors"):
            raise RuntimeError(json.dumps(payload["errors"]))

        user = payload["data"]["user"]
        stars = sum(r.get("stargazerCount", 0) or 0 for r in user["repositories"]["nodes"])
        contributions = user["contributionsCollection"]["contributionCalendar"]["totalContributions"]
        commits = (
            user["contributionsCollection"]["totalCommitContributions"]
            + user["contributionsCollection"]["restrictedContributionsCount"]
        )

        # Lines added/deleted: GitHub has no aggregate endpoint for this, so we
        # sum the authenticated user's additions/deletions across their own
        # repos via the REST "contributor stats" endpoint (best-effort, capped
        # to the first 20 repos to stay fast/within rate limits).
        repo_res = requests.get(
            f"https://api.github.com/users/{username}/repos",
            params={"per_page": 20, "sort": "pushed"},
            headers={"Authorization": f"token {token}"},
            timeout=30,
        )
        repos = repo_res.json()
        added = 0
        deleted = 0
        if isinstance(repos, list):
            for repo in repos[:20]:
                try:
                    stats_res = requests.get(
                        f"https://api.github.com/repos/{username}/{repo['name']}/stats/contributors",
                        headers={"Authorization": f"token {token}"},
                        timeout=30,
                    )
                    stats = stats_res.json()
                    if isinstance(stats, list):
                        me = next(
                            (s for s in stats if s.get("author") and s["author"].get("login") == username),
                            None,
                        )
                        if me:
                            for week in me["weeks"]:
                                added += week["a"]
                                deleted += week["d"]
                except Exception:
                    # skip repo on error, best-effort
                    pass

        result = {
            "contributions": f"{contributions:,}",
            "commits": f"{commits:,}",
            "stars": f"{stars:,}",
            "followers": f'{user["followers"]["totalCount"]:,}',
            "following": f'{user["following"]["totalCount"]:,}',
            "linesAdded": f"{added:,}" if added else fallback["linesAdded"],
            "linesDeleted": f"{deleted:,}" if deleted else fallback["linesDeleted"],
            "profileViews": fallback["profileViews"],  # see USAGE.md — GitHub has no official API for this
            "source": "live",
        }
        # GitHub account age — calculated fresh every run from the real
        # createdAt timestamp GitHub returns, no manual date needed.
        result.update(account_age_fields(user["createdAt"], has_time=True))
        return result
    except Exception as err:
        print(f"[generate.py] GitHub API fetch failed, using fallback numbers: {err}")
        return with_fallback_account_age({**fallback, "source": "fallback"})


# ---------------------------------------------------------------------------
# 3. helpers
# ---------------------------------------------------------------------------
def escape_xml(s):
    s = str(s)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def truncate(s, max_len):
    if len(s) > max_len:
        return s[: max_len - 1].rstrip() + "…"
    return s


# Monospace-aware truncation: given a pixel budget and font size, figure out
# how many characters actually fit, so text can NEVER run past the card edge.
CHAR_WIDTH_FACTOR = 0.6  # safe average for the monospace stack used


def max_chars_for(px_width, font_size):
    return max(4, math.floor(px_width / (font_size * CHAR_WIDTH_FACTOR)))


def fit_text(s, px_width, font_size):
    return truncate(s, max_chars_for(px_width, font_size))


# For "Label: value" lines where the label is fixed and only the value
# should be truncated to whatever pixel budget remains.
def fit_labeled_value(label, value, total_px_width, font_size):
    label_chars = len(label)
    remaining_px = total_px_width - label_chars * font_size * CHAR_WIDTH_FACTOR
    return fit_text(value, max(remaining_px, font_size * 4), font_size)


# ---------------------------------------------------------------------------
# 4. THEME PALETTES — the only place you need to touch to restyle
# ---------------------------------------------------------------------------
THEMES = {
    "dark": {
        "bg": "#0d1117",
        "titleBg": "#161b22",
        "cardBg": "#10151c",
        "border": "#30363d",
        "textPrimary": "#e6edf3",
        "textMuted": "#8b949e",
        "accent": "#39ff88",  # phosphor green
        "accent2": "#ffb454",  # amber
        "danger": "#ff6b6b",
        "asciiTop": "#a6f5c6",
        "asciiBottom": "#1c4d33",
        "scanColor": "#39ff88",
        "grainOpacity": 0.05,
    },
    "light": {
        "bg": "#ffffff",
        "titleBg": "#eef1f4",
        "cardBg": "#f6f8fa",
        "border": "#d0d7de",
        "textPrimary": "#1f2328",
        "textMuted": "#57606a",
        "accent": "#0a6e3d",  # deep terminal green
        "accent2": "#9a5b00",  # burnt amber
        "danger": "#c62828",
        "asciiTop": "#0a6e3d",
        "asciiBottom": "#a6d8bd",
        "scanColor": "#0a6e3d",
        "grainOpacity": 0.0,
    },
}

# ---------------------------------------------------------------------------
# 5. SVG BUILDER
# ---------------------------------------------------------------------------
def build_svg(theme, data):
    t = THEMES[theme]
    W = 1000

    # ---- fixed geometry -------------------------------------------------
    TITLE_H = 46  # terminal title bar
    PAD = 28  # padding inside the window (same on all four sides)
    GAP = 34  # gap between ascii panel and info column
    ASCII_COL_W = 548  # fixed width budget for the ascii panel
    RADIUS = 14

    info_x = PAD + ASCII_COL_W + GAP
    info_right = W - PAD
    info_w = info_right - info_x

    # ---- 1) lay out the right (info) column first, top to bottom --------
    # Every pushed item knows its own height, so the column height is
    # whatever the real content needs — nothing is hardcoded, so nothing
    # can silently overlap or run off the bottom.
    state = {"y": TITLE_H + PAD}
    blocks = []

    # Every block gets a baseline computed from its OWN height/font-size
    # (baseline = yTop + height - 6), so a tall line (e.g. the name) can
    # never bleed upward into the row above it, and descenders always stay
    # inside the block's own slot.
    def push(height, render):
        blocks.append({"yTop": state["y"], "height": height, "render": render})
        state["y"] += height

    def baseline_of(y_top, height):
        return y_top + height - 6

    def text_line(s, size, fill, height, weight=None, tracking=0, suffix=""):
        value = fit_text(s, info_w, size)
        weight_attr = f'font-weight="{weight}"' if weight else ""

        def render(y_top, value=value, size=size, fill=fill, height=height, weight_attr=weight_attr, tracking=tracking, suffix=suffix):
            return (
                f'<text x="{info_x}" y="{baseline_of(y_top, height)}" font-size="{size}" '
                f'{weight_attr} letter-spacing="{tracking}" fill="{fill}">'
                f'{escape_xml(value)}{suffix}</text>'
            )

        push(height, render)

    def labeled_line(label, value, size, height, value_color=None):
        safe_value = fit_labeled_value(label + ": ", str(value), info_w, size)
        vc = value_color or t["textPrimary"]

        def render(y_top, label=label, safe_value=safe_value, size=size, height=height, vc=vc):
            return (
                f'<text x="{info_x}" y="{baseline_of(y_top, height)}" font-size="{size}">'
                f'<tspan fill="{t["accent2"]}">{escape_xml(label)}: </tspan>'
                f'<tspan fill="{vc}">{escape_xml(safe_value)}</tspan></text>'
            )

        push(height, render)

    def divider(height):
        def render(y_top, height=height):
            return (
                f'<line x1="{info_x}" y1="{y_top + height / 2}" x2="{info_right}" '
                f'y2="{y_top + height / 2}" stroke="{t["border"]}" stroke-width="1"/>'
            )

        push(height, render)

    def section_header(text_svg, height=26, size=13.5):
        def render(y_top, text_svg=text_svg, height=height, size=size):
            return (
                f'<text x="{info_x}" y="{baseline_of(y_top, height)}" font-size="{size}" '
                f'letter-spacing="1" fill="{t["accent2"]}">{text_svg}</text>'
            )

        push(height, render)

    def wrap_items(label, items, size, total_px_width):
        """Greedy-pack comma-separated items into lines that fit
        total_px_width, so a long list wraps instead of getting cut off
        with '…'."""
        max_chars = max_chars_for(total_px_width, size)
        label_chars = len(label) + 2  # "Label: "
        first_line_budget = max(4, max_chars - label_chars)

        lines = []
        current = ""
        budget = first_line_budget
        for item in items:
            candidate = f"{current}, {item}" if current else item
            if len(candidate) <= budget:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = item
                budget = max_chars  # every line after the first uses full width
        if current:
            lines.append(current)
        return lines or [""]

    def labeled_wrapped_line(label, items, size, line_height):
        lines = wrap_items(label, items, size, info_w)
        height = line_height * len(lines)

        def render(y_top, lines=lines, label=label, size=size, line_height=line_height):
            parts = []
            for i, line_text in enumerate(lines):
                baseline = baseline_of(y_top + line_height * i, line_height)
                if i == 0:
                    parts.append(
                        f'<text x="{info_x}" y="{baseline}" font-size="{size}">'
                        f'<tspan fill="{t["accent2"]}">{escape_xml(label)}: </tspan>'
                        f'<tspan fill="{t["textPrimary"]}">{escape_xml(line_text)}</tspan></text>'
                    )
                else:
                    parts.append(
                        f'<text x="{info_x}" y="{baseline}" font-size="{size}" '
                        f'fill="{t["textPrimary"]}">{escape_xml(line_text)}</text>'
                    )
            return "\n      ".join(parts)

        push(height, render)

    # shell prompt line, then name / role / tagline
    def render_prompt(y_top):
        user_host = f'{config.get("githubUsername", "me")}@github'
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 24)}" font-size="13.5">'
            f'<tspan fill="{t["textMuted"]}">{escape_xml(user_host)}</tspan>'
            f'<tspan fill="{t["accent"]}">:~$</tspan>'
            f'<tspan fill="{t["textPrimary"]}"> ./profile --render</tspan></text>'
        )

    push(24, render_prompt)

    text_line(config["name"], 30, t["textPrimary"], 42, weight=700)

    # thin accent underline anchoring the name
    def render_name_rule(y_top):
        return (
            f'<rect x="{info_x}" y="{y_top + 1}" width="64" height="3" rx="1.5" fill="{t["accent"]}"/>'
        )

    push(10, render_name_rule)

    def render_role(y_top):
        role_text = escape_xml(fit_text(config["role"], info_w - 26, 19))
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 30)}" font-size="19">'
            f'<tspan fill="{t["accent"]}">&gt; {role_text} </tspan>'
            f'<tspan class="cursor" fill="{t["accent"]}">█</tspan></text>'
        )

    push(30, render_role)

    if config.get("tagline"):
        # word-wrap the tagline (up to 2 lines) instead of truncating it
        TAG_FS = 14
        budget = max_chars_for(info_w, TAG_FS)
        tag_lines = [""]
        for word in config["tagline"].split():
            candidate = f"{tag_lines[-1]} {word}".strip()
            if len(candidate) <= budget:
                tag_lines[-1] = candidate
            else:
                tag_lines.append(word)
        for line in tag_lines[:2]:
            text_line(line, TAG_FS, t["textMuted"], 23)
    divider(20)

    # languages / speaks / hobbies
    BODY_FS = 16
    BODY_LH = 24
    labeled_wrapped_line("Languages", config["languagesProgramming"], BODY_FS, BODY_LH)
    labeled_wrapped_line("Tech Stack", config["techStack"], BODY_FS, BODY_LH)
    labeled_wrapped_line("Speaks", config["languagesSpoken"], BODY_FS, BODY_LH)
    labeled_wrapped_line("Hobbies", config["hobbies"], BODY_FS, BODY_LH)
    divider(20)

    # live status block
    section_header(f'// live status <tspan fill="{t["textMuted"]}" letter-spacing="0">(auto-refreshed by CI)</tspan>')
    labeled_line("Age", uptime_string, BODY_FS, BODY_LH)
    if data.get("accountAge"):
        labeled_line(
            "GitHub age",
            f'{data["accountAge"]} · joined {data.get("accountCreated", "—")}',
            BODY_FS,
            BODY_LH,
        )
    labeled_line("Contributions (yr)", data["contributions"], BODY_FS, BODY_LH)
    labeled_line("Profile views", data["profileViews"], BODY_FS, BODY_LH)
    labeled_line("Total commits", data["commits"], BODY_FS, BODY_LH)
    labeled_line("Stars earned", data["stars"], BODY_FS, BODY_LH)
    labeled_line("Followers / Following", f'{data["followers"]} / {data.get("following") or "—"}', BODY_FS, BODY_LH)

    def render_lines_changed(y_top):
        added_txt = escape_xml(fit_text(str(data["linesAdded"]), info_w / 2 - 40, BODY_FS))
        deleted_txt = escape_xml(fit_text(str(data["linesDeleted"]), info_w / 2 - 40, BODY_FS))
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, BODY_LH)}" font-size="{BODY_FS}">'
            f'<tspan fill="{t["accent2"]}">Lines changed: </tspan>'
            f'<tspan fill="{t["accent"]}">+{added_txt}</tspan>'
            f'<tspan fill="{t["textMuted"]}"> / </tspan>'
            f'<tspan fill="{t["danger"]}">-{deleted_txt}</tspan></text>'
        )

    push(BODY_LH, render_lines_changed)
    divider(20)

    # contact — one full-width row per entry (pulsing dot + fixed-width
    # label + value), so even long handles/urls never get truncated.
    section_header("$ cat contact.sh", height=26)

    contact = config.get("contact", {})
    contact_rows = [
        (label, value)
        for label, value in [
            ("Email", contact.get("email")),
            ("LinkedIn", contact.get("linkedin")),
            ("GitHub", contact.get("githubUrl")),
            ("Website", contact.get("website")),
            ("Status", "open to work"),
        ]
        if value  # a missing config field just drops the entry
    ]

    LABEL_COL_W = 95  # px reserved for the uppercase label
    for label, value in contact_rows:
        row_h = 29

        def render_row(y_top, label=label, value=value, row_h=row_h):
            base = baseline_of(y_top, row_h)
            value_txt = escape_xml(fit_text(value, info_w - 18 - LABEL_COL_W, 14.5))
            return (
                f'<g>'
                f'<circle cx="{info_x + 3.5}" cy="{base - 4.5}" r="3.5" class="live-dot" fill="{t["accent"]}"/>'
                f'<text x="{info_x + 18}" y="{base}" font-size="11.5" letter-spacing="1" fill="{t["textMuted"]}">{escape_xml(label.upper())}</text>'
                f'<text x="{info_x + 18 + LABEL_COL_W}" y="{base}" font-size="14.5" fill="{t["textPrimary"]}">{value_txt}</text>'
                f'</g>'
            )

        push(row_h, render_row)

    # Symmetric padding: the window ends exactly PAD below the last block.
    content_bottom = state["y"]
    H = content_bottom + PAD

    # ---- 2) the left panel spans the same vertical range as the info ----
    panel_x = PAD
    panel_y = TITLE_H + PAD
    panel_w = ASCII_COL_W
    panel_h = content_bottom - panel_y
    panel_bottom = panel_y + panel_h

    ascii_lines = [line.rstrip() for line in ASCII_ART[theme].replace("\r", "").split("\n") if line.strip()]
    line_count = len(ascii_lines)
    max_line_len = max(len(l) for l in ascii_lines)

    # Reserve room inside the panel for the caption (top); the portrait is
    # centered in what's left.
    caption_h = 40
    bottom_pad = 20
    ascii_avail_h = panel_h - caption_h - bottom_pad
    ascii_avail_w = panel_w - 28

    # The art was sampled for 2:1 tall monospace cells, so line-height must
    # be 2 * char-width (= 1.2 * font-size) or the portrait gets squashed.
    LH_RATIO = 2 * CHAR_WIDTH_FACTOR
    fs_by_width = ascii_avail_w / (max_line_len * CHAR_WIDTH_FACTOR)
    fs_by_height = ascii_avail_h / (line_count * LH_RATIO)
    ascii_font_size = min(fs_by_width, fs_by_height)
    ascii_line_height = ascii_font_size * LH_RATIO
    ascii_block_h = ascii_line_height * line_count
    ascii_block_w = max_line_len * ascii_font_size * CHAR_WIDTH_FACTOR

    ascii_x = panel_x + (panel_w - ascii_block_w) / 2
    ascii_text_y = panel_y + caption_h + (ascii_avail_h - ascii_block_h) / 2 + ascii_font_size

    ascii_tspans = "".join(
        f'<tspan x="{ascii_x:.1f}" dy="{0 if i == 0 else round(ascii_line_height, 2)}">{escape_xml(line)}</tspan>'
        for i, line in enumerate(ascii_lines)
    )

    blocks_svg = "\n  ".join(b["render"](b["yTop"]) for b in blocks)

    divider_x = panel_x + panel_w + GAP / 2

    svg = f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape_xml(config['name'])} — developer profile card">
  <defs>
    <linearGradient id="asciiFade-{theme}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{t['asciiTop']}"/>
      <stop offset="100%" stop-color="{t['asciiBottom']}"/>
    </linearGradient>
    <linearGradient id="scanGrad-{theme}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{t['scanColor']}" stop-opacity="0"/>
      <stop offset="50%" stop-color="{t['scanColor']}" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="{t['scanColor']}" stop-opacity="0"/>
    </linearGradient>
    <clipPath id="cardClip-{theme}">
      <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="{RADIUS}"/>
    </clipPath>
    <clipPath id="asciiClip-{theme}">
      <rect x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{panel_h}" rx="10"/>
    </clipPath>
    <filter id="grain-{theme}">
      <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" stitchTiles="stitch" result="noise"/>
      <feColorMatrix in="noise" type="matrix"
        values="0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 {t['grainOpacity']} 0"/>
    </filter>
  </defs>

  <style>
    text {{ font-family: ui-monospace, SFMono-Regular, 'Cascadia Code', 'Fira Code', Menlo, Consolas, monospace; }}
    .cursor {{ animation: blink 1.1s steps(1) infinite; }}
    @keyframes blink {{ 0%, 49% {{ opacity: 1; }} 50%, 100% {{ opacity: 0; }} }}
    .scanline {{ animation: scan 5.2s linear infinite; }}
    @keyframes scan {{ 0% {{ transform: translateY(0); }} 100% {{ transform: translateY({panel_h}px); }} }}
    .live-dot {{ animation: pulse 1.6s ease-in-out infinite; transform-origin: center; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.25; }} }}
  </style>

  <!-- ============ TERMINAL WINDOW ============ -->
  <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="{RADIUS}" fill="{t['bg']}" stroke="{t['border']}" stroke-width="1.5"/>
  <g clip-path="url(#cardClip-{theme})">
    <rect x="1" y="1" width="{W - 2}" height="{TITLE_H}" fill="{t['titleBg']}"/>
  </g>
  <line x1="1" y1="{TITLE_H + 1}" x2="{W - 1}" y2="{TITLE_H + 1}" stroke="{t['border']}" stroke-width="1"/>
  <circle cx="26" cy="{TITLE_H / 2 + 1}" r="6.5" fill="#ff5f56"/>
  <circle cx="48" cy="{TITLE_H / 2 + 1}" r="6.5" fill="#ffbd2e"/>
  <circle cx="70" cy="{TITLE_H / 2 + 1}" r="6.5" fill="#27c93f"/>
  <text x="{W / 2}" y="{TITLE_H / 2 + 5.5}" text-anchor="middle" font-size="13" letter-spacing="0.5" fill="{t['textMuted']}">{escape_xml(config.get('githubUsername', 'me'))}@github: ~/profile</text>

  <!-- ============ LEFT: ASCII ART PANEL ============ -->
  <rect x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{panel_h}" rx="10" fill="{t['cardBg']}" stroke="{t['border']}" stroke-width="1"/>
  <text x="{panel_x + 18}" y="{panel_y + 27}" font-size="13.5" fill="{t['textMuted']}">$ cat ascii_{theme}.txt <tspan fill="{t['accent']}">— ok</tspan></text>
  <g clip-path="url(#asciiClip-{theme})">
    <text x="{ascii_x:.1f}" y="{ascii_text_y:.1f}" xml:space="preserve" font-size="{ascii_font_size:.2f}" letter-spacing="0" fill="url(#asciiFade-{theme})">{ascii_tspans}</text>
    <rect class="scanline" x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{round(panel_h * 0.1)}" fill="url(#scanGrad-{theme})"/>
    <rect x="{panel_x}" y="{panel_y}" width="{panel_w}" height="{panel_h}" filter="url(#grain-{theme})"/>
  </g>

  <line x1="{divider_x}" y1="{panel_y + 8}" x2="{divider_x}" y2="{panel_bottom - 8}" stroke="{t['border']}" stroke-width="1"/>

  <!-- ============ RIGHT: INFO COLUMN (dynamically laid out) ============ -->
  {blocks_svg}
</svg>"""

    return svg


# ---------------------------------------------------------------------------
# 6. main
# ---------------------------------------------------------------------------
def main():
    data = fetch_github_stats(config.get("githubUsername"))

    out_dir = os.path.join(HERE, "dist")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "profile-card-dark.svg"), "w", encoding="utf-8") as f:
        f.write(build_svg("dark", data))

    with open(os.path.join(out_dir, "profile-card-light.svg"), "w", encoding="utf-8") as f:
        f.write(build_svg("light", data))

    print("[generate.py] Wrote dist/profile-card-dark.svg and dist/profile-card-light.svg")
    print(f"[generate.py] Uptime: {uptime_string}")
    print(f"[generate.py] Stats source: {data['source']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(err, file=sys.stderr)
        sys.exit(1)
