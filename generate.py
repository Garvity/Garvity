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

Run in CI (see .github/workflows/update-readme.yml):
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

with open(os.path.join(HERE, "ascii_dark.txt"), "r", encoding="utf-8") as f:
    ascii_raw = f.read()


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
        "accountCreated": created_dt.strftime("%b %d, %Y"),
        "accountAge": f'{parts["y"]}y {parts["m"]}m {parts["d"]}d',
        "accountAgeDays": f'{parts["totalDays"]:,} days',
    }


age = duration_parts(datetime.strptime(config["birthDate"], "%Y-%m-%d"))
uptime_string = f'{age["y"]}y {age["m"]}m {age["d"]}d'
uptime_days_string = f'{age["totalDays"]:,} days alive'


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
            nodes {
              stargazerCount
              defaultBranchRef {
                target {
                  ... on Commit {
                    history(author: { id: "me" }) { totalCount }
                  }
                }
              }
            }
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
        "cardBg": "#11161d",
        "border": "#27303b",
        "textPrimary": "#e6edf3",
        "textMuted": "#7d8590",
        "accent": "#39ff88",  # phosphor green
        "accent2": "#ffb454",  # amber
        "danger": "#ff6b6b",
        "asciiTop": "#a6f5c6",
        "asciiBottom": "#1c4d33",
        "scanColor": "#39ff88",
        "grainOpacity": 0.05,
    },
    "light": {
        "bg": "#f6f8fa",
        "cardBg": "#ffffff",
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
    OUTER_PAD = 24  # margin around the whole card
    INNER_PAD = 22  # padding inside the card
    GAP = 46  # gap between ascii column and info column
    ASCII_COL_W = 420  # fixed width budget for the ascii column
    info_x = OUTER_PAD + INNER_PAD + ASCII_COL_W + GAP
    info_right = W - OUTER_PAD - INNER_PAD
    info_w = info_right - info_x

    # ---- 1) lay out the right (info) column first, top to bottom --------
    # Every pushed item knows its own height, so the column height is
    # whatever the real content needs — nothing is hardcoded, so nothing
    # can silently overlap or run off the bottom.
    state = {"y": OUTER_PAD + INNER_PAD}
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

    # name / role / tagline
    text_line(config["name"], 27, t["textPrimary"], 36, weight=700)

    def render_role(y_top):
        role_text = escape_xml(fit_text(config["role"], info_w - 20, 14))
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 22)}" font-size="14">'
            f'<tspan fill="{t["accent"]}">&gt; {role_text} </tspan>'
            f'<tspan class="cursor" fill="{t["accent"]}">\u2588</tspan></text>'
        )

    push(22, render_role)

    if config.get("tagline"):
        text_line(config["tagline"], 11, t["textMuted"], 20)
    divider(16)

    # languages / speaks / hobbies
    labeled_wrapped_line("Languages", config["languagesProgramming"], 12.5, 18)
    labeled_wrapped_line("Tech Stack", config["techStack"], 12.5, 18)
    labeled_wrapped_line("Speaks", config["languagesSpoken"], 12.5, 18)
    labeled_wrapped_line("Hobbies", config["hobbies"], 12.5, 18)
    divider(16)

    # live status block
    def render_status_header(y_top):
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 17)}" font-size="11" fill="{t["accent2"]}">'
            f'// live status <tspan fill="{t["textMuted"]}">(auto-refreshed by CI)</tspan></text>'
        )

    push(17, render_status_header)
    labeled_line("Age", f"{uptime_string} \u00b7 {uptime_days_string}", 12.5, 18)
    if data.get("accountAge"):
        labeled_line(
            "GitHub age",
            f'{data["accountAge"]} \u00b7 since {data.get("accountCreated", "\u2014")}',
            12.5,
            18,
        )
    labeled_line("Contributions (yr)", data["contributions"], 12.5, 18)
    labeled_line("Profile views", data["profileViews"], 12.5, 18)
    labeled_line("Total commits", data["commits"], 12.5, 18)
    labeled_line("Stars earned", data["stars"], 12.5, 18)
    labeled_line("Followers / Following", f'{data["followers"]} / {data.get("following") or "\u2014"}', 12.5, 18)

    def render_lines_changed(y_top):
        added_txt = escape_xml(fit_text(str(data["linesAdded"]), info_w / 2 - 40, 12.5))
        deleted_txt = escape_xml(fit_text(str(data["linesDeleted"]), info_w / 2 - 40, 12.5))
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 18)}" font-size="12.5">'
            f'<tspan fill="{t["accent2"]}">Lines changed: </tspan>'
            f'<tspan fill="{t["accent"]}">+{added_txt}</tspan>'
            f'<tspan fill="{t["textMuted"]}"> / </tspan>'
            f'<tspan fill="{t["danger"]}">-{deleted_txt}</tspan></text>'
        )

    push(18, render_lines_changed)
    divider(16)

    # contact — folded into the same column, 2 items per row so long
    # handles/urls each get their own half-width budget and can't collide.
    def render_contact_header(y_top):
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 15)}" font-size="11" fill="{t["accent2"]}">'
            f"$ cat contact.sh</text>"
        )

    push(15, render_contact_header)

    col_w = (info_w - 24) / 2
    col2_x = info_x + col_w + 24
    contact = config["contact"]
    contact_pairs = [
        ("Email", contact.get("email")),
        ("LinkedIn", contact.get("linkedin")),
        ("GitHub", contact.get("githubUrl")),
        # ("Twitter / X", contact.get("twitter")),
        ("Website", contact.get("website")),
        ("Status", "open to work"),
    ]

    for i in range(0, len(contact_pairs), 2):
        row_h = 32
        label_a, value_a = contact_pairs[i]
        label_b, value_b = contact_pairs[i + 1] if i + 1 < len(contact_pairs) else (None, None)

        def render_row(y_top, label_a=label_a, value_a=value_a, label_b=label_b, value_b=value_b, row_h=row_h):
            label_y = y_top + 12
            value_y = y_top + 26
            part_b = ""
            if label_b:
                value_b_txt = escape_xml(fit_text(value_b, col_w - 12, 11.5))
                part_b = f"""
        <circle cx="{col2_x + 3}" cy="{label_y - 3}" r="3" class="live-dot" fill="{t['accent']}"/>
        <text x="{col2_x + 12}" y="{label_y}" font-size="9" letter-spacing="0.5" fill="{t['textMuted']}">{escape_xml(label_b.upper())}</text>
        <text x="{col2_x + 12}" y="{value_y}" font-size="11.5" fill="{t['textPrimary']}">{value_b_txt}</text>"""
            value_a_txt = escape_xml(fit_text(value_a, col_w - 12, 11.5))
            return f"""
      <g>
        <circle cx="{info_x + 3}" cy="{label_y - 3}" r="3" class="live-dot" fill="{t['accent']}"/>
        <text x="{info_x + 12}" y="{label_y}" font-size="9" letter-spacing="0.5" fill="{t['textMuted']}">{escape_xml(label_a.upper())}</text>
        <text x="{info_x + 12}" y="{value_y}" font-size="11.5" fill="{t['textPrimary']}">{value_a_txt}</text>
        {part_b}
      </g>"""

        push(row_h, render_row)

    content_bottom = state["y"] + INNER_PAD  # bottom padding under the last item
    card_h = content_bottom - OUTER_PAD
    H = card_h + OUTER_PAD * 2

    # ---- 2) size the ascii block to exactly fill that same height -------
    ascii_lines = [line for line in ascii_raw.replace("\r", "").split("\n") if line]
    line_count = len(ascii_lines)
    max_line_len = max(len(l) for l in ascii_lines)
    ascii_avail_h = card_h - INNER_PAD * 2 - 14  # leave room for the caption line
    ascii_avail_w = ASCII_COL_W - 16
    lh_by_height = ascii_avail_h / line_count
    fs_by_width = ascii_avail_w / (max_line_len * CHAR_WIDTH_FACTOR)
    ascii_font_size = min(lh_by_height / 1.15, fs_by_width, 11)  # never bigger than 11px, and never bigger than either budget allows
    ascii_line_height = ascii_font_size * 1.15
    ascii_block_h = ascii_line_height * line_count
    ascii_x = OUTER_PAD + INNER_PAD + 8
    ascii_box_y = OUTER_PAD + INNER_PAD
    ascii_box_h = card_h - INNER_PAD * 2
    ascii_text_y = ascii_box_y + (ascii_box_h - 14 - ascii_block_h) / 2 + ascii_font_size  # vertically centered, room left for caption

    ascii_tspans = "".join(
        f'<tspan x="{ascii_x}" dy="{0 if i == 0 else ascii_line_height}">{escape_xml(line)}</tspan>'
        for i, line in enumerate(ascii_lines)
    )

    ascii_clip_y = OUTER_PAD
    ascii_clip_h = card_h

    blocks_svg = "\n  ".join(b["render"](b["yTop"]) for b in blocks)

    svg = f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape_xml(config['name'])} — developer profile card">
  <defs>
    <linearGradient id="asciiFade-{theme}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{t['asciiTop']}"/>
      <stop offset="100%" stop-color="{t['asciiBottom']}"/>
    </linearGradient>
    <linearGradient id="scanGrad-{theme}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{t['scanColor']}" stop-opacity="0"/>
      <stop offset="50%" stop-color="{t['scanColor']}" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="{t['scanColor']}" stop-opacity="0"/>
    </linearGradient>
    <clipPath id="asciiClip-{theme}">
      <rect x="{OUTER_PAD}" y="{ascii_clip_y}" width="{ASCII_COL_W + INNER_PAD * 2}" height="{ascii_clip_h}" rx="8"/>
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
    .scanline {{ animation: scan 4.8s linear infinite; }}
    @keyframes scan {{ 0% {{ transform: translateY(0); }} 100% {{ transform: translateY({ascii_clip_h}px); }} }}
    .live-dot {{ animation: pulse 1.6s ease-in-out infinite; transform-origin: center; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.25; }} }}
  </style>

  <rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="16" fill="{t['bg']}" stroke="{t['border']}" stroke-width="1.5"/>

  <!-- ============ LEFT: ASCII ART ============ -->
  <rect x="{OUTER_PAD}" y="{ascii_clip_y}" width="{ASCII_COL_W + INNER_PAD * 2}" height="{ascii_clip_h}" rx="8" fill="{t['cardBg']}" stroke="{t['border']}" stroke-width="1"/>
  <g clip-path="url(#asciiClip-{theme})">
    <text x="{ascii_x}" y="{ascii_text_y}" xml:space="preserve" font-size="{ascii_font_size:.2f}" letter-spacing="0.1" fill="url(#asciiFade-{theme})">{ascii_tspans}</text>
    <rect class="scanline" x="{OUTER_PAD}" y="{ascii_clip_y}" width="{ASCII_COL_W + INNER_PAD * 2}" height="{round(ascii_clip_h * 0.11)}" fill="url(#scanGrad-{theme})"/>
    <rect x="{OUTER_PAD}" y="{ascii_clip_y}" width="{ASCII_COL_W + INNER_PAD * 2}" height="{ascii_clip_h}" filter="url(#grain-{theme})"/>
  </g>
  <text x="{ascii_x}" y="{ascii_clip_y + ascii_clip_h - 10}" font-size="9.5" fill="{t['textMuted']}">$ file ascii_dark.png <tspan fill="{t['accent']}">— ok</tspan></text>

  <line x1="{OUTER_PAD + ASCII_COL_W + INNER_PAD * 2 + GAP / 2}" y1="{ascii_clip_y + 10}" x2="{OUTER_PAD + ASCII_COL_W + INNER_PAD * 2 + GAP / 2}" y2="{ascii_clip_y + ascii_clip_h - 10}" stroke="{t['border']}" stroke-width="1"/>

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
    print(f"[generate.py] Uptime: {uptime_string} ({uptime_days_string})")
    print(f"[generate.py] Stats source: {data['source']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        print(err, file=sys.stderr)
        sys.exit(1)