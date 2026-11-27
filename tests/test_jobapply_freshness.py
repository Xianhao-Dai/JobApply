"""Behavioral checks for discovery dates and reposts, without the live ledger."""
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jobapply_freshness import dispositions, normalize_repost, source_repost_floor, source_time_floor


NOW = datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


class RepostFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE job_sources (
                job_id TEXT, source_key TEXT, source_listing_id TEXT,
                published_at_as_listed TEXT, first_seen_at TEXT, last_seen_at TEXT,
                verification_state TEXT, reposted_at TEXT, reposted_at_basis TEXT,
                repost_observed_at TEXT, repost_label TEXT
            );
            CREATE TABLE manual_job_leads (job_id TEXT, status TEXT);
            CREATE TABLE job_freshness_rechecks (
                job_id TEXT, observed_at TEXT, live_state TEXT, listed_by_at TEXT
            );
        """)
        self.job = {"job_id": "old-role", "listing_state": "open", "closes_at": None,
                    "published_at": "2026-08-31T22:22:27Z", "published_at_basis": "official_first_published",
                    "first_seen_at": "2026-09-01T00:00:00Z", "last_verified_at": STAMP}

    def tearDown(self):
        self.db.close()

    def source(self, raw="Reposted 2 hours ago", **fields):
        source = {"job_id": self.job["job_id"], "source_key": "linkedin:jobs", "source_listing_id": "123",
                  "published_at_as_listed": raw, "first_seen_at": "2026-09-01T00:00:00Z",
                  "last_seen_at": STAMP, "verification_state": "verified", **fields}
        columns = list(source)
        self.db.execute("INSERT INTO job_sources (" + ",".join(columns) + ") VALUES (" +
                        ",".join("?" for _ in columns) + ")", list(source.values()))
        return source

    def state(self, now=NOW):
        return dispositions(self.db, [self.job], now)[self.job["job_id"]]

    def reset_history(self):
        self.db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT)')
        self.db.execute('INSERT INTO settings VALUES (?,?)', ('last_data_reset', json.dumps({
            'at': STAMP, 'historical_job_ids': [self.job['job_id']]})))

    def test_reset_tracking_dependency_cannot_reenter_from_publication_date(self):
        self.reset_history()
        self.job.update(published_at=STAMP, first_seen_at=STAMP)
        self.assertEqual(self.state(), 'historical_tracking')

    def test_reset_requires_new_verified_approved_source_observation(self):
        self.reset_history()
        self.source(last_seen_at=(NOW - timedelta(minutes=1)).isoformat())
        self.assertEqual(self.state(), 'historical_tracking')
        self.db.execute('UPDATE job_sources SET last_seen_at=?, verification_state=?', (STAMP, 'unverified'))
        self.assertEqual(self.state(), 'historical_tracking')
        self.db.execute("UPDATE job_sources SET verification_state='verified'")
        self.assertEqual(self.state(), 'active')

    def test_repost_reactivates_old_official_post_without_mutating_it(self):
        self.source()
        self.db.execute("INSERT INTO job_freshness_rechecks VALUES (?,?,?,?)",
                        (self.job["job_id"], STAMP, "open", "2026-09-01T00:00:00Z"))
        before = self.job.copy()
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.job, before)

    def test_closed_and_elapsed_deadline_take_precedence_over_repost_and_manual(self):
        self.source()
        self.db.execute("INSERT INTO manual_job_leads VALUES (?,?)", (self.job["job_id"], "verified"))
        self.job["listing_state"] = "closed"
        self.assertEqual(self.state(), "closed")
        self.job["listing_state"] = "open"
        self.job["closes_at"] = (NOW - timedelta(seconds=1)).isoformat()
        self.assertEqual(self.state(), "closed")

    def test_newest_official_status_wins_against_older_audit(self):
        self.source()
        earlier = (NOW - timedelta(hours=1)).isoformat()
        self.db.execute("INSERT INTO job_freshness_rechecks VALUES (?,?,?,?)",
                        (self.job["job_id"], earlier, "closed", None))
        self.assertEqual(self.state(), "active")
        self.db.execute("UPDATE job_freshness_rechecks SET observed_at=?", (STAMP,))
        self.assertEqual(self.state(), "closed")

    def test_stale_fixed_repost_expires_despite_recent_last_seen(self):
        fixed = normalize_repost({"published_at_as_listed": "Reposted 2 hours ago"}, STAMP)
        source = self.source(**fixed)
        self.assertEqual(self.state(), "active")
        later = NOW + timedelta(hours=24)
        self.db.execute("UPDATE job_sources SET last_seen_at=?", (later.isoformat(),))
        self.assertEqual(source_repost_floor({**source, "last_seen_at": later.isoformat()}),
                         NOW - timedelta(hours=3))
        self.assertEqual(self.state(later), "older_than_24h")

    def test_repeated_identical_relative_label_keeps_prior_anchor(self):
        first = normalize_repost({"published_at_as_listed": "Reposted 2 hours ago"}, STAMP)
        prior = self.source(**first)
        later = NOW + timedelta(hours=48)
        repeated = normalize_repost({"published_at_as_listed": "Reposted 2 hours ago"}, later.isoformat(), prior)
        self.assertEqual(first, repeated)
        self.db.execute("UPDATE job_sources SET last_seen_at=?", (later.isoformat(),))
        self.assertEqual(self.state(later), "older_than_24h")

    def test_legacy_label_is_pinned_when_ingested_again(self):
        prior = self.source(raw="Reposted22hoursago")
        later = NOW + timedelta(hours=2)
        fixed = normalize_repost({"published_at_as_listed": "Reposted22hoursago"}, later.isoformat(), prior)
        self.assertEqual(fixed["repost_observed_at"], STAMP)
        self.assertEqual(source_repost_floor({**prior, **fixed}), NOW - timedelta(hours=23))

    def test_equivalent_label_format_cannot_refresh_the_anchor(self):
        prior = self.source(raw="Reposted22hoursago")
        fixed = normalize_repost({"published_at_as_listed": "REPOSTED 22 hours ago"},
                                 (NOW + timedelta(hours=48)).isoformat(), prior)
        self.assertEqual(fixed["repost_observed_at"], STAMP)
        self.assertEqual(source_repost_floor({**prior, **fixed}), NOW - timedelta(hours=23))

    def test_ambiguous_day_and_update_time_do_not_count_as_repost(self):
        for raw in ("Reposted 1 day ago", "Reposted 1d", "Updated 1 hour ago"):
            with self.subTest(raw=raw):
                self.db.execute("DELETE FROM job_sources")
                source = self.source(raw=raw)
                self.assertEqual(normalize_repost(source, STAMP), {})
                self.assertEqual(self.state(), "older_than_24h")

    def test_future_and_malformed_explicit_repost_is_rejected(self):
        for fields in ({"reposted_at": (NOW + timedelta(minutes=1)).isoformat()},
                       {"reposted_at": "not-a-date"},
                       {"reposted_at_basis": "updated_at"},
                       {"repost_observed_at": (NOW + timedelta(minutes=1)).isoformat()}):
            candidate = {"reposted_at": (NOW - timedelta(hours=2)).isoformat(),
                         "reposted_at_basis": "source_exact", "repost_observed_at": STAMP,
                         "repost_label": "Reposted", **fields}
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    normalize_repost(candidate, STAMP)
                self.assertIsNone(source_repost_floor({**candidate, "last_seen_at": STAMP}))

    def test_explicit_milliseconds_survive_ledger_second_precision(self):
        observed = NOW.replace(microsecond=888000)
        candidate = {"observed_at": observed.isoformat(),
                     "reposted_at": (observed - timedelta(hours=12)).isoformat(),
                     "reposted_at_basis": "source_relative_lower_bound",
                     "repost_observed_at": observed.isoformat(), "repost_label": "Reposted 11 hours ago"}
        fixed = normalize_repost(candidate, STAMP)
        self.assertEqual(datetime.fromisoformat(fixed["repost_observed_at"]), NOW)
        self.assertEqual(source_repost_floor({**fixed, "last_seen_at": STAMP}), NOW - timedelta(hours=12))

    def test_auto_millisecond_label_is_self_consistent_and_keeps_its_anchor(self):
        observed = NOW.replace(microsecond=888000).isoformat()
        first = normalize_repost({"published_at_as_listed": "Reposted 2 hours ago"}, observed)
        prior = {**first, "last_seen_at": STAMP}
        self.assertEqual(source_repost_floor(prior), NOW - timedelta(hours=3))
        repeated = normalize_repost({"published_at_as_listed": "Reposted 2 hours ago"},
                                    (NOW + timedelta(hours=48)).isoformat(), prior)
        self.assertEqual(repeated, first)

    def test_existing_fractional_anchor_is_normalized_without_refreshing_it(self):
        observed = NOW.replace(microsecond=888000)
        prior = {"reposted_at": (observed - timedelta(hours=3)).isoformat(),
                 "reposted_at_basis": "source_relative_lower_bound",
                 "repost_observed_at": observed.isoformat(), "repost_label": "Reposted 2 hours ago",
                 "last_seen_at": observed.isoformat()}
        later = (NOW + timedelta(hours=48)).isoformat()
        fixed = normalize_repost({"published_at_as_listed": "Reposted 2 hours ago"}, later, prior)
        self.assertEqual(datetime.fromisoformat(fixed["repost_observed_at"]), NOW)
        self.assertEqual(source_repost_floor({**fixed, "last_seen_at": later}), NOW - timedelta(hours=3))

    def test_exact_millisecond_time_is_conservatively_truncated_without_relative_inference(self):
        observed = NOW.replace(microsecond=888000)
        posted = (NOW - timedelta(hours=1, seconds=17)).replace(microsecond=555000)
        fixed = normalize_repost({"observed_at": observed.isoformat(), "reposted_at": posted.isoformat(),
                                 "reposted_at_basis": "source_exact", "repost_observed_at": observed.isoformat(),
                                 "repost_label": "Reposted " + posted.isoformat()}, STAMP)
        self.assertEqual(fixed["reposted_at_basis"], "source_exact")
        self.assertEqual(source_repost_floor({**fixed, "last_seen_at": STAMP}), posted.replace(microsecond=0))

    def test_prior_explicit_fields_keep_legacy_raw_label_fallback(self):
        prior = {"reposted_at": (NOW - timedelta(hours=3)).isoformat(),
                 "reposted_at_basis": "source_relative_lower_bound", "repost_observed_at": STAMP,
                 "repost_label": None, "published_at_as_listed": "Reposted 2 hours ago", "last_seen_at": STAMP}
        later = (NOW + timedelta(hours=48)).isoformat()
        fixed = normalize_repost({"published_at_as_listed": "Reposted 2 hours ago"}, later, prior)
        self.assertEqual(fixed["repost_observed_at"], STAMP)
        self.assertEqual(source_repost_floor({**fixed, "last_seen_at": later}), NOW - timedelta(hours=3))

    def test_precision_normalization_does_not_hide_future_or_invalid_evidence(self):
        observed = NOW.replace(microsecond=100000)
        candidate = {"observed_at": observed.isoformat(),
                     "reposted_at": (observed - timedelta(hours=12)).isoformat(),
                     "reposted_at_basis": "source_relative_lower_bound",
                     "repost_observed_at": observed.isoformat(), "repost_label": "Reposted 11 hours ago"}
        cases = (
            {"repost_observed_at": NOW.replace(microsecond=900000).isoformat(),
             "reposted_at": (NOW.replace(microsecond=900000) - timedelta(hours=12)).isoformat()},
            {"reposted_at": (observed - timedelta(hours=11)).isoformat()},
            {"reposted_at_basis": "updated_at"},
            {"repost_label": "Reposted 1d"},
            {"reposted_at_basis": "source_exact", "reposted_at": NOW.replace(microsecond=900000).isoformat()},
            {"observed_at": (observed + timedelta(seconds=1)).isoformat(),
             "repost_observed_at": (observed + timedelta(seconds=1)).isoformat(),
             "reposted_at": (observed + timedelta(seconds=1) - timedelta(hours=12)).isoformat()},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_repost({**candidate, **changes}, STAMP)
        # A fractional observed_at argument is already precise; a conflicting raw
        # value in the same second cannot extend its genuine observation time.
        with self.assertRaises(ValueError):
            normalize_repost({**candidate, "observed_at": NOW.replace(microsecond=900000).isoformat(),
                              **cases[0]}, observed.isoformat())

    def test_unverified_source_does_not_reactivate(self):
        self.source(verification_state="unverified")
        self.assertEqual(self.state(), "older_than_24h")

    def test_manual_and_new_original_post_behavior_is_preserved(self):
        self.assertEqual(self.state(), "older_than_24h")
        self.db.execute("INSERT INTO manual_job_leads VALUES (?,?)", (self.job["job_id"], "verified"))
        self.assertEqual(self.state(), "active")
        self.db.execute("DELETE FROM manual_job_leads")
        self.job["published_at"] = (NOW - timedelta(hours=1)).isoformat()
        self.job["first_seen_at"] = STAMP
        self.assertEqual(self.state(), "active")

    def test_rounding_boundary_uses_older_edge(self):
        self.source(raw="Reposted 23 hours ago")
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.state(NOW + timedelta(seconds=1)), "older_than_24h")
        self.assertEqual(source_time_floor({"published_at_as_listed": "3 hours ago", "last_seen_at": STAMP}),
                         NOW - timedelta(hours=4))


if __name__ == "__main__":
    unittest.main()
