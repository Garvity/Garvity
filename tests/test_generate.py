import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import generate


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise generate.REQUEST_EXCEPTION_TYPES[0](f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


USER_ID = "U_owner"


def repository_node(repository_id, name, head_oid, stars=0):
    return {
        "id": repository_id,
        "name": name,
        "owner": {"login": "owner"},
        "stargazerCount": stars,
        "defaultBranchRef": {"target": {"oid": head_oid}},
    }


def graphql_payload(nodes, has_next=False, cursor=None):
    return {
        "data": {
            "user": {
                "id": USER_ID,
                "createdAt": "2020-01-15T12:30:00Z",
                "followers": {"totalCount": 12},
                "following": {"totalCount": 4},
                "repositories": {
                    "totalCount": 101,
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                },
                "contributionsCollection": {
                    "contributionCalendar": {"totalContributions": 345},
                    "totalCommitContributions": 120,
                },
            }
        }
    }


def history_commit(oid, additions, deletions, author_id=USER_ID):
    return {
        "oid": oid,
        "additions": additions,
        "deletions": deletions,
        "author": {"user": {"id": author_id}} if author_id else {"user": None},
    }


def history_payload(commits, has_next=False, cursor=None):
    return {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "target": {
                        "history": {
                            "nodes": commits,
                            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                        }
                    }
                }
            }
        }
    }


class DurationTests(unittest.TestCase):
    def test_duration_uses_calendar_boundaries(self):
        self.assertEqual(
            generate.duration_parts(date(2024, 1, 31), date(2024, 3, 1)),
            {"y": 0, "m": 1, "d": 1, "totalDays": 30},
        )

    def test_duration_clamps_leap_day_anniversary(self):
        self.assertEqual(
            generate.duration_parts(date(2024, 2, 29), date(2025, 2, 28)),
            {"y": 1, "m": 0, "d": 0, "totalDays": 365},
        )

    def test_duration_ignores_time_of_day(self):
        parts = generate.duration_parts(
            datetime(2024, 1, 1, 23, 59),
            datetime(2024, 1, 2, 0, 1),
        )
        self.assertEqual(parts, {"y": 0, "m": 0, "d": 1, "totalDays": 1})

    def test_account_age_accepts_iso_offset(self):
        fields = generate.account_age_fields(
            "2020-01-15T12:30:00+00:00",
            now=date(2022, 3, 20),
        )
        self.assertEqual(fields["accountCreated"], "Jan 2020")
        self.assertEqual(fields["accountAge"], "2y 2m")
        self.assertEqual(fields["accountAgeDays"], "795 days")


class TextHelperTests(unittest.TestCase):
    def test_truncate_handles_tiny_budgets(self):
        self.assertEqual(generate.truncate("abc", 0), "")
        self.assertEqual(generate.truncate("abc", 1), "…")

    def test_wrapped_items_never_exceed_budget(self):
        width = 120
        size = 10
        lines = generate.wrap_items("Label", ["x" * 100, "short"], size, width)
        max_chars = generate.max_chars_for(width, size)
        self.assertTrue(all(len(line) <= max_chars for line in lines))

    def test_wrap_text_limits_line_count(self):
        lines = generate.wrap_text("one two three four five six", 10, max_lines=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 10 for line in lines))


class GraphQLTests(unittest.TestCase):
    def test_repository_pages_are_accumulated(self):
        session = FakeSession(
            [
                FakeResponse(
                    graphql_payload(
                        [repository_node("R1", "one", "head-one", 2), repository_node("R2", "two", "head-two", 3)],
                        has_next=True,
                        cursor="next",
                    )
                ),
                FakeResponse(graphql_payload([repository_node("R3", "three", "head-three", 7)])),
            ]
        )
        result = generate._fetch_graphql_stats("owner", "token", session)
        self.assertEqual(result["stars"], 12)
        self.assertEqual(result["repositories"], 101)
        self.assertEqual(result["commits"], 120)
        self.assertEqual(result["userId"], USER_ID)
        self.assertEqual(len(result["repositoriesForLines"]), 3)
        self.assertEqual(session.calls[0][1]["json"]["variables"]["cursor"], None)
        self.assertEqual(session.calls[1][1]["json"]["variables"]["cursor"], "next")

    def test_transient_server_error_is_retried(self):
        session = FakeSession(
            [
                FakeResponse({}, status_code=500),
                FakeResponse(graphql_payload([1])),
            ]
        )
        sleeps = []
        data = generate._graphql_request(
            session,
            {"Authorization": "bearer token"},
            generate.GITHUB_STATS_QUERY,
            {"login": "owner", "cursor": None},
            sleep_fn=sleeps.append,
        )
        self.assertIn("user", data)
        self.assertEqual(sleeps, [1])

    def test_graphql_errors_fail_with_context(self):
        session = FakeSession([FakeResponse({"errors": [{"message": "bad query"}]})])
        with self.assertRaisesRegex(generate.GitHubStatsError, "bad query"):
            generate._graphql_request(
                session,
                {"Authorization": "bearer token"},
                generate.GITHUB_STATS_QUERY,
                {"login": "owner", "cursor": None},
                sleep_fn=lambda _: None,
            )


class LineStatsCacheTests(unittest.TestCase):
    def setUp(self):
        self.repository = {
            "id": "R1",
            "owner": "owner",
            "name": "project",
            "headOid": "new-head",
        }
        self.headers = {"Authorization": "bearer token"}

    def test_unchanged_repository_uses_cached_totals_without_request(self):
        cache = generate._empty_stats_cache("owner")
        cache["repositories"]["R1"] = {
            "headOid": "new-head",
            "linesAdded": 100,
            "linesDeleted": 20,
        }
        session = FakeSession([])
        totals, updated = generate._collect_line_stats([self.repository], USER_ID, cache, session, self.headers)
        self.assertEqual(totals, (100, 20))
        self.assertEqual(updated, cache)
        self.assertEqual(session.calls, [])

    def test_incremental_history_counts_only_commits_after_cached_head(self):
        cache = generate._empty_stats_cache("owner")
        cache["repositories"]["R1"] = {
            "headOid": "old-head",
            "linesAdded": 100,
            "linesDeleted": 20,
        }
        session = FakeSession(
            [
                FakeResponse(
                    history_payload(
                        [
                            history_commit("new-head", 10, 3),
                            history_commit("someone-else", 50, 30, "U_other"),
                            history_commit("old-head", 4, 1),
                        ]
                    )
                )
            ]
        )
        totals, updated = generate._collect_line_stats([self.repository], USER_ID, cache, session, self.headers)
        self.assertEqual(totals, (110, 23))
        self.assertEqual(updated["repositories"]["R1"], {
            "headOid": "new-head",
            "linesAdded": 110,
            "linesDeleted": 23,
        })

    def test_rewritten_history_rebuilds_repository_total(self):
        cache = generate._empty_stats_cache("owner")
        cache["repositories"]["R1"] = {
            "headOid": "missing-old-head",
            "linesAdded": 100,
            "linesDeleted": 20,
        }
        session = FakeSession(
            [
                FakeResponse(
                    history_payload(
                        [
                            history_commit("new-head", 8, 2),
                            history_commit("older", 6, 1),
                            history_commit("other", 100, 50, "U_other"),
                        ]
                    )
                )
            ]
        )
        totals, _ = generate._collect_line_stats([self.repository], USER_ID, cache, session, self.headers)
        self.assertEqual(totals, (14, 3))

    def test_malformed_cache_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(generate.load_stats_cache(path, "owner"), generate._empty_stats_cache("owner"))


class ConfigAndOutputTests(unittest.TestCase):
    def test_current_config_is_valid(self):
        generate.validate_config(generate.load_config())

    def test_invalid_list_reports_field_name(self):
        profile_config = generate.load_config()
        profile_config["techStack"] = []
        with self.assertRaisesRegex(generate.ConfigError, "config.techStack"):
            generate.validate_config(profile_config)

    def test_atomic_write_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.svg"
            path.write_text("old", encoding="utf-8")
            generate.write_text_atomic(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            self.assertEqual(list(Path(directory).glob(".output.svg.*.tmp")), [])

    def test_fallback_uses_cached_account_wide_counts(self):
        profile_config = generate.load_config()
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            cache = generate._empty_stats_cache("owner")
            cache["repositories"]["R1"] = {
                "headOid": "head",
                "linesAdded": 100,
                "linesDeleted": 20,
            }
            generate.write_stats_cache(cache_path, cache)
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
                result = generate.fetch_github_stats("owner", profile_config, cache_path=cache_path)
        self.assertEqual(result["linesAdded"], "100")
        self.assertEqual(result["linesDeleted"], "20")
        self.assertEqual(result["lineStatsSource"], "cache")

    def test_account_age_is_rendered_below_personal_age(self):
        profile_config = generate.load_config()
        ascii_art = generate.load_ascii_art()
        svg = generate.build_svg(
            "dark",
            {**profile_config["statsFallback"], "accountAge": "6y 2m", "accountCreated": "May 2020"},
            profile_config,
            ascii_art,
            "21y 9m 22d",
        )
        self.assertLess(svg.index("Age: "), svg.index("Account age: "))


if __name__ == "__main__":
    unittest.main()
