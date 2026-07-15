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

import calendar
import json
import logging
import math
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

if requests is not None:
    REQUEST_EXCEPTION_TYPES = (requests.RequestException,)
else:
    class RequestTransportError(Exception):
        """Testable transport error used when requests is unavailable."""

    REQUEST_EXCEPTION_TYPES = (RequestTransportError,)

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATS_CACHE_PATH = HERE / "dist" / "profile-card-stats.json"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
LOGGER = logging.getLogger("generate.py")

REQUIRED_LIST_FIELDS = (
    "languagesProgramming",
    "techStack",
    "languagesSpoken",
    "hobbies",
)
REQUIRED_FALLBACK_FIELDS = (
    "contributions",
    "commits",
    "stars",
    "followers",
    "following",
    "linesAdded",
    "linesDeleted",
    "profileViews",
)


class ConfigError(ValueError):
    """Raised when project configuration is missing or invalid."""


class GitHubStatsError(RuntimeError):
    """Raised when GitHub statistics cannot be fetched safely."""


def _require_nonempty_string(mapping, key, context="config"):
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")


def validate_config(profile_config):
    """Validate fields consumed by the renderer and statistics fetcher."""
    if not isinstance(profile_config, dict):
        raise ConfigError("config.json must contain a JSON object")

    for key in ("name", "role", "githubUsername", "birthDate"):
        _require_nonempty_string(profile_config, key)

    try:
        birth_date = date.fromisoformat(profile_config["birthDate"])
    except ValueError as err:
        raise ConfigError("config.birthDate must use YYYY-MM-DD format") from err
    if birth_date > date.today():
        raise ConfigError("config.birthDate cannot be in the future")

    for key in REQUIRED_LIST_FIELDS:
        items = profile_config.get(key)
        if not isinstance(items, list) or not items:
            raise ConfigError(f"config.{key} must be a non-empty list")
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ConfigError(f"config.{key} items must be non-empty strings")

    contact = profile_config.get("contact")
    if not isinstance(contact, dict):
        raise ConfigError("config.contact must be an object")
    for key, value in contact.items():
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"config.contact.{key} must be a non-empty string or null")

    fallback = profile_config.get("statsFallback")
    if not isinstance(fallback, dict):
        raise ConfigError("config.statsFallback must be an object")
    for key in REQUIRED_FALLBACK_FIELDS:
        if key not in fallback or not isinstance(fallback[key], (str, int)):
            raise ConfigError(f"config.statsFallback.{key} must be a string or integer")
        if isinstance(fallback[key], str) and not fallback[key].strip():
            raise ConfigError(f"config.statsFallback.{key} cannot be empty")

    fallback_created = fallback.get("accountCreated")
    if fallback_created:
        if not isinstance(fallback_created, str):
            raise ConfigError("config.statsFallback.accountCreated must use YYYY-MM-DD format")
        try:
            date.fromisoformat(fallback_created)
        except ValueError as err:
            raise ConfigError("config.statsFallback.accountCreated must use YYYY-MM-DD format") from err


def load_config(path=CONFIG_PATH):
    try:
        with Path(path).open("r", encoding="utf-8") as config_file:
            profile_config = json.load(config_file)
    except FileNotFoundError as err:
        raise ConfigError(f"configuration file not found: {path}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(
            f"invalid JSON in {path} at line {err.lineno}, column {err.colno}: {err.msg}"
        ) from err

    validate_config(profile_config)
    return profile_config


def load_ascii_art(base_dir=HERE):
    """Load and validate the theme-specific ASCII portraits."""
    art = {}
    for theme in ("dark", "light"):
        path = Path(base_dir) / f"ascii_{theme}.txt"
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError as err:
            raise ConfigError(f"ASCII art file not found: {path}") from err
        if not any(line.strip() for line in content.splitlines()):
            raise ConfigError(f"ASCII art file is empty: {path}")
        art[theme] = content
    return art


# ---------------------------------------------------------------------------
# 1. AGE / "UPTIME" — computed fresh every time this script runs
# ---------------------------------------------------------------------------
def _as_date(value, field_name):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"{field_name} must be a date or datetime")


def _add_calendar_years(value, years):
    target_year = value.year + years
    target_day = min(value.day, calendar.monthrange(target_year, value.month)[1])
    return value.replace(year=target_year, day=target_day)


def _add_calendar_months(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    target_year, zero_based_month = divmod(month_index, 12)
    target_month = zero_based_month + 1
    target_day = min(value.day, calendar.monthrange(target_year, target_month)[1])
    return value.replace(year=target_year, month=target_month, day=target_day)


def duration_parts(start_dt, now=None):
    """Return completed calendar years/months/days and elapsed total days."""
    start_date = _as_date(start_dt, "start_dt")
    end_date = _as_date(now, "now") if now is not None else date.today()
    if start_date > end_date:
        raise ValueError("start_dt cannot be in the future")

    y = end_date.year - start_date.year
    if _add_calendar_years(start_date, y) > end_date:
        y -= 1
    year_anchor = _add_calendar_years(start_date, y)

    m = (end_date.year - year_anchor.year) * 12 + end_date.month - year_anchor.month
    if _add_calendar_months(year_anchor, m) > end_date:
        m -= 1
    month_anchor = _add_calendar_months(year_anchor, m)
    d = (end_date - month_anchor).days

    total_days = (end_date - start_date).days
    return {"y": y, "m": m, "d": d, "totalDays": total_days}


def account_age_fields(created_iso, has_time=True, now=None):
    """Given an ISO date (GitHub createdAt, e.g. 2019-06-01T12:00:00Z) or a
    plain YYYY-MM-DD fallback date, return formatted account-age fields."""
    if not isinstance(created_iso, str) or not created_iso.strip():
        raise ValueError("created_iso must be a non-empty ISO-8601 string")
    try:
        if has_time:
            created_date = datetime.fromisoformat(created_iso.replace("Z", "+00:00")).date()
        else:
            created_date = date.fromisoformat(created_iso)
    except ValueError as err:
        raise ValueError(f"invalid account creation date: {created_iso}") from err

    parts = duration_parts(created_date, now=now)
    return {
        "accountCreated": created_date.strftime("%b %Y"),
        "accountAge": f'{parts["y"]}y {parts["m"]}m',
        "accountAgeDays": f'{parts["totalDays"]:,} days',
    }


# ---------------------------------------------------------------------------
# 2. GITHUB STATS — account data and owned-repository line changes
# ---------------------------------------------------------------------------
STATS_CACHE_VERSION = 1

GITHUB_STATS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    id
    createdAt
    followers { totalCount }
    following { totalCount }
    repositories(
      first: 100
      after: $cursor
      ownerAffiliations: OWNER
      isFork: false
      orderBy: {field: NAME, direction: ASC}
    ) {
      totalCount
      nodes {
        id
        name
        owner { login }
        stargazerCount
        defaultBranchRef {
          target {
            ... on Commit { oid }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
    contributionsCollection {
      contributionCalendar { totalContributions }
      totalCommitContributions
    }
  }
}
"""

REPOSITORY_HISTORY_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor) {
            nodes {
              oid
              additions
              deletions
              author { user { id } }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
  }
}
"""


def format_count(value):
    """Format integer-like values with grouping while preserving text fallbacks."""
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _empty_stats_cache(username):
    return {
        "version": STATS_CACHE_VERSION,
        "username": username,
        "repositories": {},
    }


def load_stats_cache(path, username):
    """Load valid cache entries; malformed or mismatched caches are ignored."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_stats_cache(username)
    except (OSError, json.JSONDecodeError) as err:
        LOGGER.warning("Could not read line-stat cache; rebuilding it: %s", err)
        return _empty_stats_cache(username)

    if (
        not isinstance(payload, dict)
        or payload.get("version") != STATS_CACHE_VERSION
        or payload.get("username") != username
        or not isinstance(payload.get("repositories"), dict)
    ):
        LOGGER.warning("Line-stat cache is incompatible; rebuilding it")
        return _empty_stats_cache(username)

    repositories = {}
    for repository_id, entry in payload["repositories"].items():
        if (
            isinstance(repository_id, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("headOid"), str)
            and isinstance(entry.get("linesAdded"), int)
            and isinstance(entry.get("linesDeleted"), int)
        ):
            repositories[repository_id] = {
                "headOid": entry["headOid"],
                "linesAdded": entry["linesAdded"],
                "linesDeleted": entry["linesDeleted"],
            }
    return {
        "version": STATS_CACHE_VERSION,
        "username": username,
        "repositories": repositories,
    }


def write_stats_cache(path, cache):
    write_text_atomic(path, json.dumps(cache, indent=2, sort_keys=True) + "\n")


def _cache_totals(cache):
    entries = cache.get("repositories", {}).values()
    if not entries:
        return None
    return (
        sum(entry["linesAdded"] for entry in entries),
        sum(entry["linesDeleted"] for entry in entries),
    )


def _fallback_stats(fallback, cache=None):
    result = {**fallback, "source": "fallback"}
    cache_totals = _cache_totals(cache or {})
    if cache_totals is not None:
        result["linesAdded"] = format_count(cache_totals[0])
        result["linesDeleted"] = format_count(cache_totals[1])
        result["lineStatsSource"] = "cache"
    else:
        result["lineStatsSource"] = "fallback"

    fallback_created = fallback.get("accountCreated")
    if fallback_created:
        result.update(account_age_fields(fallback_created, has_time=False))
    return result


def _retry_delay(response, attempt):
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(2 ** (attempt - 1), 8)


def _is_retryable_response(response):
    if response is None:
        return True
    if response.status_code == 429 or response.status_code >= 500:
        return True
    return response.status_code == 403 and (
        response.headers.get("Retry-After") is not None
        or response.headers.get("X-RateLimit-Remaining") == "0"
    )


def _graphql_request(session, headers, query, variables, sleep_fn=time.sleep, max_attempts=3):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        response = None
        try:
            response = session.post(
                GITHUB_GRAPHQL_URL,
                headers=headers,
                json={"query": query, "variables": variables},
                timeout=30,
            )
            if _is_retryable_response(response) and response.status_code >= 400:
                response.raise_for_status()
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise GitHubStatsError("GitHub returned a non-object JSON response")
            if payload.get("errors"):
                raise GitHubStatsError(f"GitHub GraphQL errors: {json.dumps(payload['errors'])}")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise GitHubStatsError("GitHub GraphQL response did not include data")
            return data
        except GitHubStatsError:
            raise
        except REQUEST_EXCEPTION_TYPES + (ValueError,) as err:
            last_error = err
            retryable = _is_retryable_response(response)
            if not retryable or attempt == max_attempts:
                break
            delay = _retry_delay(response, attempt)
            LOGGER.warning(
                "GitHub request attempt %d/%d failed; retrying in %.1fs: %s",
                attempt,
                max_attempts,
                delay,
                err,
            )
            sleep_fn(delay)
    raise GitHubStatsError(f"GitHub request failed after {max_attempts} attempts: {last_error}")


def _graphql_headers(token):
    return {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Garvity-profile-card-generator",
    }


def _fetch_graphql_stats(username, token, session, sleep_fn=time.sleep):
    headers = _graphql_headers(token)
    cursor = None
    stars = 0
    first_user = None
    seen_cursors = set()
    repositories_for_lines = []

    while True:
        data = _graphql_request(
            session,
            headers,
            GITHUB_STATS_QUERY,
            {"login": username, "cursor": cursor},
            sleep_fn=sleep_fn,
        )
        user = data.get("user")
        if not isinstance(user, dict):
            raise GitHubStatsError(f"GitHub user not found: {username}")
        if first_user is None:
            first_user = user

        repositories = user.get("repositories")
        if not isinstance(repositories, dict):
            raise GitHubStatsError("GitHub response omitted repository statistics")
        nodes = repositories.get("nodes") or []
        if not isinstance(nodes, list):
            raise GitHubStatsError("GitHub returned invalid repository nodes")
        stars += sum(
            repo.get("stargazerCount", 0) or 0
            for repo in nodes
            if isinstance(repo, dict)
        )
        for repo in nodes:
            if not isinstance(repo, dict):
                continue
            owner = repo.get("owner") or {}
            target = ((repo.get("defaultBranchRef") or {}).get("target") or {})
            if not isinstance(owner.get("login"), str) or not isinstance(target.get("oid"), str):
                continue
            repositories_for_lines.append(
                {
                    "id": repo.get("id"),
                    "owner": owner["login"],
                    "name": repo.get("name"),
                    "headOid": target["oid"],
                }
            )

        page_info = repositories.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen_cursors:
            raise GitHubStatsError("GitHub repository pagination returned an invalid cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    contributions = first_user.get("contributionsCollection") or {}
    calendar_data = contributions.get("contributionCalendar") or {}
    try:
        contribution_count = int(calendar_data["totalContributions"])
        commit_count = int(contributions["totalCommitContributions"])
        follower_count = int(first_user["followers"]["totalCount"])
        following_count = int(first_user["following"]["totalCount"])
        repository_count = int(first_user["repositories"]["totalCount"])
        created_at = first_user["createdAt"]
        user_id = first_user["id"]
    except (KeyError, TypeError, ValueError) as err:
        raise GitHubStatsError("GitHub response omitted required statistics") from err

    return {
        "contributions": contribution_count,
        "commits": commit_count,
        "stars": stars,
        "followers": follower_count,
        "following": following_count,
        "repositories": repository_count,
        "createdAt": created_at,
        "userId": user_id,
        "repositoriesForLines": [
            repo
            for repo in repositories_for_lines
            if isinstance(repo["id"], str) and isinstance(repo["name"], str)
        ],
    }


def _fetch_repository_line_stats(repository, user_id, cached_entry, session, headers, sleep_fn):
    """Fetch changed commits until the cached default-branch head is reached."""
    cached_head = cached_entry.get("headOid") if cached_entry else None
    additions = 0
    deletions = 0
    found_cached_head = False
    cursor = None
    seen_cursors = set()

    while True:
        data = _graphql_request(
            session,
            headers,
            REPOSITORY_HISTORY_QUERY,
            {"owner": repository["owner"], "name": repository["name"], "cursor": cursor},
            sleep_fn=sleep_fn,
        )
        api_repository = data.get("repository") or {}
        target = ((api_repository.get("defaultBranchRef") or {}).get("target") or {})
        history = target.get("history") or {}
        nodes = history.get("nodes") or []
        if not isinstance(nodes, list):
            raise GitHubStatsError(f"GitHub returned invalid history for {repository['owner']}/{repository['name']}")

        for commit in nodes:
            if not isinstance(commit, dict):
                continue
            if cached_head and commit.get("oid") == cached_head:
                found_cached_head = True
                break
            author = (commit.get("author") or {}).get("user") or {}
            if author.get("id") != user_id:
                continue
            try:
                additions += int(commit.get("additions", 0) or 0)
                deletions += int(commit.get("deletions", 0) or 0)
            except (TypeError, ValueError) as err:
                raise GitHubStatsError(
                    f"GitHub returned invalid line counts for {repository['owner']}/{repository['name']}"
                ) from err

        if found_cached_head:
            break
        page_info = history.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor in seen_cursors:
            raise GitHubStatsError(
                f"GitHub returned an invalid history cursor for {repository['owner']}/{repository['name']}"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if cached_entry and found_cached_head:
        additions += cached_entry["linesAdded"]
        deletions += cached_entry["linesDeleted"]
    elif cached_entry:
        LOGGER.info(
            "Cached head was not reachable for %s/%s; rebuilding its line totals",
            repository["owner"],
            repository["name"],
        )

    return {
        "headOid": repository["headOid"],
        "linesAdded": additions,
        "linesDeleted": deletions,
    }


def _collect_line_stats(repositories, user_id, cache, session, headers, sleep_fn=time.sleep):
    entries = {}
    for repository in repositories:
        cached_entry = cache["repositories"].get(repository["id"])
        if cached_entry and cached_entry["headOid"] == repository["headOid"]:
            entries[repository["id"]] = cached_entry
            continue
        entries[repository["id"]] = _fetch_repository_line_stats(
            repository,
            user_id,
            cached_entry,
            session,
            headers,
            sleep_fn,
        )

    updated_cache = {
        "version": STATS_CACHE_VERSION,
        "username": cache["username"],
        "repositories": entries,
    }
    totals = _cache_totals(updated_cache) or (0, 0)
    return totals, updated_cache


def fetch_github_stats(username, profile_config, cache_path=STATS_CACHE_PATH, session=None, sleep_fn=time.sleep):
    token = os.environ.get("GITHUB_TOKEN")
    fallback = profile_config["statsFallback"]
    cache = load_stats_cache(cache_path, username)

    if not token or not username:
        LOGGER.warning("No GITHUB_TOKEN/githubUsername set; using configured API fallbacks")
        return _fallback_stats(fallback, cache)
    if requests is None:
        LOGGER.warning("The requests package is unavailable; using configured API fallbacks")
        return _fallback_stats(fallback, cache)

    owns_session = session is None
    api_session = session or requests.Session()
    try:
        live = _fetch_graphql_stats(username, token, api_session, sleep_fn=sleep_fn)
        line_totals, updated_cache = _collect_line_stats(
            live["repositoriesForLines"],
            live["userId"],
            cache,
            api_session,
            _graphql_headers(token),
            sleep_fn=sleep_fn,
        )
        try:
            write_stats_cache(cache_path, updated_cache)
        except OSError as err:
            LOGGER.warning("Could not update line-stat cache: %s", err)
        result = {
            "contributions": format_count(live["contributions"]),
            "commits": format_count(live["commits"]),
            "stars": format_count(live["stars"]),
            "followers": format_count(live["followers"]),
            "following": format_count(live["following"]),
            "repositories": format_count(live["repositories"]),
            "profileViews": format_count(fallback["profileViews"]),
            "source": "live",
            "linesAdded": format_count(line_totals[0]),
            "linesDeleted": format_count(line_totals[1]),
            "lineStatsSource": "github",
        }
        result.update(account_age_fields(live["createdAt"], has_time=True))
        return result
    except (GitHubStatsError, KeyError, TypeError, ValueError) as err:
        LOGGER.warning("GitHub API fetch failed; using configured fallbacks: %s", err)
        return _fallback_stats(fallback, cache)
    finally:
        if owns_session:
            api_session.close()


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
    s = str(s)
    if max_len <= 0:
        return ""
    if max_len == 1:
        return "…" if s else ""
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


def wrap_items(label, items, size, total_px_width):
    """Greedily pack comma-separated items without exceeding line budgets."""
    max_chars = max_chars_for(total_px_width, size)
    first_line_budget = max(4, max_chars - len(label) - 2)
    lines = []
    current = ""
    budget = first_line_budget

    for raw_item in items:
        item = str(raw_item)
        candidate = f"{current}, {item}" if current else item
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
            budget = max_chars
        if len(item) > budget:
            lines.append(truncate(item, budget))
            budget = max_chars
        else:
            current = item

    if current:
        lines.append(current)
    return lines or [""]


def wrap_text(text, max_chars, max_lines=None):
    """Wrap words and truncate an overlong final line deterministically."""
    words = str(text).split()
    if not words:
        return [""]

    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word

    if current:
        lines.append(current)

    lines = [truncate(line, max_chars) for line in lines]
    if max_lines is not None and len(lines) > max_lines:
        kept = lines[:max_lines]
        remainder = " ".join(lines[max_lines - 1 :])
        kept[-1] = truncate(remainder, max_chars)
        return kept
    return lines


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
def build_svg(theme, data, profile_config, ascii_art, uptime_string):
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
        user_host = f'{profile_config.get("githubUsername", "me")}@github'
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 24)}" font-size="13.5">'
            f'<tspan fill="{t["textMuted"]}">{escape_xml(user_host)}</tspan>'
            f'<tspan fill="{t["accent"]}">:~$</tspan>'
            f'<tspan fill="{t["textPrimary"]}"> ./profile --render</tspan></text>'
        )

    push(24, render_prompt)

    text_line(profile_config["name"], 30, t["textPrimary"], 42, weight=700)

    # thin accent underline anchoring the name
    def render_name_rule(y_top):
        return (
            f'<rect x="{info_x}" y="{y_top + 1}" width="64" height="3" rx="1.5" fill="{t["accent"]}"/>'
        )

    push(10, render_name_rule)

    def render_role(y_top):
        role_text = escape_xml(fit_text(profile_config["role"], info_w - 26, 19))
        return (
            f'<text x="{info_x}" y="{baseline_of(y_top, 30)}" font-size="19">'
            f'<tspan fill="{t["accent"]}">&gt; {role_text} </tspan>'
            f'<tspan class="cursor" fill="{t["accent"]}">█</tspan></text>'
        )

    push(30, render_role)

    if profile_config.get("tagline"):
        # word-wrap the tagline (up to 2 lines) instead of truncating it
        TAG_FS = 14
        budget = max_chars_for(info_w, TAG_FS)
        tag_lines = wrap_text(profile_config["tagline"], budget, max_lines=2)
        for line in tag_lines:
            text_line(line, TAG_FS, t["textMuted"], 23)
    divider(20)

    # languages / speaks / hobbies
    BODY_FS = 16
    BODY_LH = 24
    labeled_wrapped_line("Languages", profile_config["languagesProgramming"], BODY_FS, BODY_LH)
    labeled_wrapped_line("Tech Stack", profile_config["techStack"], BODY_FS, BODY_LH)
    labeled_wrapped_line("Speaks", profile_config["languagesSpoken"], BODY_FS, BODY_LH)
    labeled_wrapped_line("Hobbies", profile_config["hobbies"], BODY_FS, BODY_LH)
    divider(20)

    # live status block
    section_header(f'// live status <tspan fill="{t["textMuted"]}" letter-spacing="0">(auto-refreshed by CI)</tspan>')
    labeled_line("Age", uptime_string, BODY_FS, BODY_LH)
    if data.get("accountAge"):
        labeled_line(
            "Account age",
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

    contact = profile_config.get("contact", {})
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

    ascii_lines = [line.rstrip() for line in ascii_art[theme].replace("\r", "").split("\n") if line.strip()]
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

    svg = f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape_xml(profile_config['name'])} — developer profile card">
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
  <text x="{W / 2}" y="{TITLE_H / 2 + 5.5}" text-anchor="middle" font-size="13" letter-spacing="0.5" fill="{t['textMuted']}">{escape_xml(profile_config.get('githubUsername', 'me'))}@github: ~/profile</text>

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
def write_text_atomic(path, content):
    """Replace a UTF-8 text file only after its full contents are written."""
    path = Path(path)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main():
    profile_config = load_config()
    ascii_art = load_ascii_art()
    birth_date = date.fromisoformat(profile_config["birthDate"])
    age = duration_parts(birth_date)
    uptime_string = f'{age["y"]}y {age["m"]}m {age["d"]}d'
    data = fetch_github_stats(profile_config["githubUsername"], profile_config)

    outputs = {
        "dark": build_svg("dark", data, profile_config, ascii_art, uptime_string),
        "light": build_svg("light", data, profile_config, ascii_art, uptime_string),
    }

    out_dir = HERE / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    for theme, svg in outputs.items():
        write_text_atomic(out_dir / f"profile-card-{theme}.svg", svg)

    LOGGER.info("Wrote dist/profile-card-dark.svg and dist/profile-card-light.svg")
    LOGGER.info("Uptime: %s", uptime_string)
    LOGGER.info(
        "Stats source: GitHub=%s, lines=%s",
        data["source"],
        data.get("lineStatsSource", "fallback"),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[generate.py] %(levelname)s: %(message)s")
    try:
        main()
    except (ConfigError, OSError, ValueError) as err:
        LOGGER.error("%s", err)
        sys.exit(1)
    except Exception:
        LOGGER.exception("Unexpected generation failure")
        sys.exit(1)
