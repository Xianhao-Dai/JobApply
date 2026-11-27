"""Age out job opportunities without overwriting their publication or application history."""
import argparse
import json
import re
import sqlite3
from hashlib import sha256
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
WINDOW = timedelta(hours=24)
SOURCE_CONFIG_REVISION = 5
CURRENT_SOURCES = {"glassdoor:jobs", "linkedin:jobs", "newgrad-jobs:us", "speedyapply:2027-us-newgrad"}
TRACKING_STATES = {"submission_pending_verification", "submitted", "interviewing", "rejected", "offer", "withdrawn"}
OFFICIAL_BASES = {"official_first_published", "official_last_published", "official_date_only"}
REPOST_BASES = {"source_exact", "source_relative_lower_bound"}
REPOST_FIELDS = ("reposted_at", "reposted_at_basis", "repost_observed_at", "repost_label")
WORKFLOW_RETENTION_POLICY = "qualified_until_closed_or_withdrawn"


def application_needs_tracking(application):
    """Keep unresolved prior applications visible after the discovery window."""
    return bool(application and (application.get("state") in TRACKING_STATES or
                                 application.get("blocker_code") == "POSSIBLE_DUPLICATE_APPLICATION"))


def utc_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except ValueError:
        return None


def date_bounds(value):
    try:
        day = date.fromisoformat(value or "")
    except ValueError:
        return None
    # A date without a source timezone could be anywhere in the civil UTC−12 to UTC+14 range.
    start_utc = datetime.combine(day, time.min, timezone.utc)
    return start_utc - timedelta(hours=14), start_utc + timedelta(hours=36)


def _relative_time_floor(raw, observed):
    """Use the older edge of a rounded minute/hour, never a guessed day."""
    if not observed:
        return None
    exact = utc_timestamp(raw)
    if exact:
        return exact if exact <= observed else None
    match = re.fullmatch(r"(?:posted\s*)?(\d+)\s*(m|min|minute|minutes|h|hr|hour|hours)(?:\s*ago)?", raw, flags=re.I)
    if not match:
        return None
    quantity, unit = match.groups()
    factor = 60 if unit.lower().startswith("h") else 1
    try:
        return observed - timedelta(minutes=(int(quantity) + 1) * factor)
    except OverflowError:
        return None


def _repost_label(source):
    explicit = source.get("repost_label")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    raw = source.get("published_at_as_listed")
    return raw.strip() if isinstance(raw, str) and re.match(r"reposted(?=\s|\d|:|$)", raw.strip(), re.I) else None


def _repost_label_floor(label, observed):
    raw = re.sub(r"^reposted\s*:?\s*(?:on\s+)?", "", label, flags=re.I)
    return _relative_time_floor(raw, observed)


def source_repost_floor(source):
    """Read fixed repost evidence, with a compatibility path for old raw labels."""
    if source.get("verification_state") not in (None, "verified"):
        return None
    if any(source.get(field) for field in REPOST_FIELDS[:3]):
        posted = utc_timestamp(source.get("reposted_at"))
        observed = utc_timestamp(source.get("repost_observed_at"))
        last_seen = utc_timestamp(source.get("last_seen_at"))
        basis = source.get("reposted_at_basis")
        if not posted or not observed or basis not in REPOST_BASES or posted > observed:
            return None
        if last_seen and observed > last_seen:
            return None
        if basis == "source_relative_lower_bound":
            label = _repost_label(source)
            if not label or _repost_label_floor(label, observed) != posted:
                return None
        return posted
    label = _repost_label(source)
    observed = utc_timestamp(source.get("last_seen_at"))
    return _repost_label_floor(label, observed) if label else None


def _repost_fields_at_seconds(floor, basis, anchor_at, label):
    """Conservatively match the ledger's second precision after validation."""
    anchor = utc_timestamp(anchor_at)
    whole_anchor = anchor.replace(microsecond=0)
    floor = (_repost_label_floor(label, whole_anchor) if basis == "source_relative_lower_bound"
             else floor.replace(microsecond=0))
    return {"reposted_at": floor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reposted_at_basis": basis,
            "repost_observed_at": whole_anchor.strftime("%Y-%m-%dT%H:%M:%SZ") if anchor.microsecond else anchor_at,
            "repost_label": label}


def normalize_repost(observation, observed_at, prior=None):
    """Normalize a real source observation without turning a scan into a repost.

    Explicit fields keep their original observation time. Re-reading the same
    relative label keeps the prior anchor; merely refreshing last_seen_at cannot
    move an old repost into today's window. Ambiguous labels return no fields.
    """
    observed = utc_timestamp(observed_at)
    if not observed:
        raise ValueError("Repost observation requires a timezone-aware observed_at")
    # Ledger ingestion truncates its observed_at argument before calling us.
    # Recover only the same raw UTC second, so genuine future evidence is still
    # rejected at its original precision before canonical timestamps are floored.
    raw_observed = utc_timestamp(observation.get("observed_at"))
    if not observed.microsecond and raw_observed and raw_observed.replace(microsecond=0) == observed:
        observed = raw_observed
    label = _repost_label(observation)
    has_explicit = any(observation.get(field) for field in REPOST_FIELDS[:3])
    if has_explicit:
        candidate = {field: observation.get(field) for field in REPOST_FIELDS}
        candidate["last_seen_at"] = observed.isoformat()
        floor = source_repost_floor(candidate)
        if not floor:
            raise ValueError("Invalid repost time, basis, or source observation")
        return _repost_fields_at_seconds(floor, candidate["reposted_at_basis"],
                                        candidate["repost_observed_at"], candidate["repost_label"])
    if not label:
        return {}
    prior = dict(prior) if prior else {}
    same_label = re.sub(r"\s+", "", _repost_label(prior) or "").casefold() == re.sub(r"\s+", "", label).casefold()
    if same_label and (floor := source_repost_floor(prior)):
        if prior.get("reposted_at"):
            return _repost_fields_at_seconds(floor, prior["reposted_at_basis"],
                                            prior["repost_observed_at"], _repost_label(prior))
        anchor = prior.get("last_seen_at")
    else:
        anchor = observed_at
        floor = _repost_label_floor(label, observed)
    if not floor:
        return {}
    raw = re.sub(r"^reposted\s*:?\s*(?:on\s+)?", "", label, flags=re.I)
    basis = "source_exact" if utc_timestamp(raw) else "source_relative_lower_bound"
    return _repost_fields_at_seconds(floor, basis, anchor, label)


def source_time_floor(source):
    if _repost_label(source) or any(source.get(field) for field in REPOST_FIELDS[:3]):
        return source_repost_floor(source)
    raw = (source.get("published_at_as_listed") or "").strip()
    return _relative_time_floor(raw, utc_timestamp(source.get("last_seen_at")))


def visible_lead(source, now):
    if source["source_key"] not in CURRENT_SOURCES:
        return False
    floor = source_time_floor(source)
    if floor:
        return now - WINDOW <= floor <= now
    observed = utc_timestamp(source.get("last_seen_at"))
    label = (source.get("published_at_as_listed") or "").strip().lower()
    label = re.sub(r"^reposted\s*:?\s*", "", label)
    return bool(observed and now - WINDOW <= observed <= now and label in {
        "today", "yesterday", "1d", "1 day ago", "24 hours ago", "new", "just posted",
    })


def exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def dispositions(db, jobs, now):
    audits = {}
    if exists(db, "job_freshness_rechecks"):
        audits = {r["job_id"]: dict(r) for r in db.execute("SELECT * FROM job_freshness_rechecks")}
    manual = set()
    if exists(db, "manual_job_leads"):
        manual = {r[0] for r in db.execute("SELECT job_id FROM manual_job_leads WHERE status='verified' AND job_id IS NOT NULL")}
    sources = defaultdict(list)
    source_keys = sorted(CURRENT_SOURCES)
    placeholders = ", ".join("?" for _ in source_keys)
    for row in db.execute(f"""SELECT * FROM job_sources WHERE job_id IS NOT NULL
             AND source_key IN ({placeholders})""", source_keys):
        sources[row["job_id"]].append(dict(row))
    cutoff = now - WINDOW
    reset_row = db.execute("SELECT value_json FROM settings WHERE key='last_data_reset'").fetchone() if exists(db, 'settings') else None
    reset = json.loads(reset_row[0]) if reset_row else {}
    reset_at = utc_timestamp(reset.get('at'))
    historical_ids = set(reset.get('historical_job_ids', []))
    result = {}
    for job in jobs:
        key = job["job_id"]
        # Tracking dependencies survive an administrative reset. They are not
        # new opportunities until a new approved-source observation or manual
        # intake verifies them again. Publication age alone cannot revive them.
        reobserved = reset_at and any(
            source.get('verification_state') == 'verified' and
            (seen := utc_timestamp(source.get('last_seen_at'))) and seen >= reset_at
            for source in sources[key])
        if key in historical_ids and key not in manual and not reobserved:
            result[key] = 'historical_tracking'
            continue
        closes = utc_timestamp(job.get("closes_at"))
        audit = audits.get(key)
        verified_at = utc_timestamp(job.get("last_verified_at"))
        live = job["listing_state"]
        if audit and (not verified_at or utc_timestamp(audit["observed_at"]) >= verified_at):
            live = audit["live_state"]
        basis = job.get("published_at_basis")
        published_at = utc_timestamp(job.get("published_at"))
        known_listed = utc_timestamp(audit.get("listed_by_at")) if audit else None
        first_seen_at = utc_timestamp(job.get("first_seen_at"))
        bounds = date_bounds(job.get("published_at")) if basis == "official_date_only" else None
        recent_repost = any(
            cutoff <= posted <= now and (observed := utc_timestamp(
                source.get("repost_observed_at") or source.get("last_seen_at"))) and observed <= now
            for source in sources[key] if (posted := source_repost_floor(source))
        )
        if live == "closed" or (closes and closes <= now):
            result[key] = "closed"
        elif key not in manual and not recent_repost and ((published_at and basis in OFFICIAL_BASES and published_at < cutoff)
                                    or (bounds and bounds[1] <= cutoff) or (known_listed and known_listed < cutoff)
                                    or (first_seen_at and first_seen_at < cutoff)):
            result[key] = "older_than_24h"
        elif live != "open":
            result[key] = "open_status_unverified"
        elif key in manual:
            result[key] = "active"
        elif recent_repost:
            result[key] = "active"
        elif basis in OFFICIAL_BASES and published_at:
            result[key] = "active" if cutoff <= published_at <= now else "publication_unverified"
        elif bounds and cutoff <= bounds[0] <= now:
            result[key] = "active"
        elif any(cutoff <= floor <= now for source in sources[key] if (floor := source_time_floor(source))):
            result[key] = "active"
        else:
            result[key] = "publication_unverified"
    return result


def workflow_policy_enabled(db, now=None):
    if not exists(db, "settings"):
        return False
    row = db.execute("SELECT value_json FROM settings WHERE key='workflow_retention_policy'").fetchone()
    if not row or json.loads(row[0]) != WORKFLOW_RETENTION_POLICY:
        return False
    if now is not None:
        enabled = db.execute("SELECT value_json FROM settings WHERE key='workflow_retention_enabled_at'").fetchone()
        if enabled and (not (stamp := utc_timestamp(json.loads(enabled[0]))) or stamp > now):
            return False
    return True


def official_availability(db, jobs, now):
    """Current official status/deadline, independent of discovery age."""
    audits = ({r["job_id"]: dict(r) for r in db.execute("SELECT * FROM job_freshness_rechecks")}
              if exists(db, "job_freshness_rechecks") else {})
    result = {}
    for job in jobs:
        live = job["listing_state"]
        audit = audits.get(job["job_id"])
        verified = utc_timestamp(job.get("last_verified_at"))
        if audit and (not verified or utc_timestamp(audit["observed_at"]) >= verified):
            live = audit["live_state"]
        closes = utc_timestamp(job.get("closes_at"))
        result[job["job_id"]] = ("closed" if live == "closed" or (closes and closes <= now) else
                                 "active" if live == "open" else "open_status_unverified")
    return result


def workflow_dispositions(db, jobs, now):
    """Retain explicitly admitted work; current materials are checked downstream.

    Admission is a durable fact, not a waiver of official status, match versions,
    application duplication guards, or the user's final Submit requirement.
    """
    jobs = list(jobs)
    result = dispositions(db, jobs, now)
    availability = official_availability(db, jobs, now)
    admitted = set()
    if workflow_policy_enabled(db, now) and exists(db, "job_workflow_admissions"):
        admitted = {r["job_id"] for r in db.execute("SELECT job_id,admitted_at FROM job_workflow_admissions")
                    if (stamp := utc_timestamp(r["admitted_at"])) and stamp <= now}
    applications = ({r["job_id"]: dict(r) for r in db.execute("SELECT * FROM applications")}
                    if exists(db, "applications") else {})
    for job in jobs:
        key = job["job_id"]
        app = applications.get(key, {})
        if app.get("state") == "withdrawn":
            result[key] = "withdrawn"
        elif app.get("state") in TRACKING_STATES:
            result[key] = "tracking"
        elif availability[key] != "active":
            result[key] = availability[key]
        elif key in admitted:
            result[key] = "active"
    return result


def discovery_snapshot_admission(db, task, result, job, created_at, now):
    """Validate eligibility at discovery, without re-anchoring a rounded age.

    The immutable accepted task/result is the authority. This fact permits only
    matching; a qualified current match must separately admit the workflow.
    """
    if not workflow_policy_enabled(db, now):
        return None
    enabled_row = db.execute("SELECT value_json FROM settings WHERE key='workflow_retention_enabled_at'").fetchone()
    enabled = utc_timestamp(json.loads(enabled_row[0])) if enabled_row else None
    data = task.get("input", {})
    since, until = utc_timestamp(data.get("since_at")), utc_timestamp(data.get("until_at"))
    created, finished = utc_timestamp(created_at), utc_timestamp(result.get("completed_at"))
    source = task.get("scope_key")
    if (task.get("stage") != "discover" or source not in CURRENT_SOURCES or data.get("source_key") != source
            or result.get("status") != "succeeded" or not enabled or not created or created < enabled
            or not since or not until or not finished or not enabled <= until <= finished <= now
            or not until - WINDOW <= since <= until
            or any(result.get(k) != task.get(k) for k in
                   ("schema_version", "task_id", "attempt", "stage", "scope_key", "job_id", "input_versions"))):
        return None
    for observation in result.get("output", {}).get("observations", []):
        if (not isinstance(observation, dict) or observation.get("listing_state") != "open"
                or observation.get("verification_state") not in (None, "verified")):
            continue
        observed = utc_timestamp(observation.get("observed_at"))
        source_id = observation.get("source_listing_id")
        mapped = db.execute("""SELECT job_id,verification_state FROM job_sources
            WHERE source_key=? AND source_listing_id=?""", (source, source_id)).fetchone()
        if (not observed or not created <= observed <= finished or observed < enabled
                or not mapped or mapped["job_id"] != job["job_id"] or mapped["verification_state"] != "verified"
                or observation.get("official_req_id") != job["official_req_id"]
                or (observation.get("company_key") and observation["company_key"] != job["company_key"])
                or not observation.get("official_url") or not observation.get("jd_path")):
            continue
        artifact = next((a for a in result.get("artifacts", []) if a.get("kind") == "jd"
                         and a.get("path") == observation["jd_path"]), None)
        if not artifact or not db.execute("SELECT 1 FROM job_jd_versions WHERE job_id=? AND sha256=?",
                                          (job["job_id"], artifact.get("sha256"))).fetchone():
            continue
        source_fact = {**observation, "last_seen_at": observation["observed_at"]}
        repost = source_repost_floor(source_fact)
        recent_repost = bool(repost and since <= repost <= until and repost <= observed)
        basis, published = job.get("published_at_basis"), utc_timestamp(job.get("published_at"))
        bounds = date_bounds(job.get("published_at")) if basis == "official_date_only" else None
        first_seen = utc_timestamp(job.get("first_seen_at"))
        audit = (db.execute("SELECT * FROM job_freshness_rechecks WHERE job_id=?", (job["job_id"],)).fetchone()
                 if exists(db, "job_freshness_rechecks") else None)
        listed = utc_timestamp(dict(audit).get("listed_by_at")) if audit else None
        if not recent_repost and ((basis in OFFICIAL_BASES and published and published < since)
                                 or (bounds and bounds[1] <= since) or (listed and listed < since)
                                 or (first_seen and first_seen < since)):
            continue
        if (basis in OFFICIAL_BASES and published and published > min(until, observed)) or (bounds and bounds[0] > min(until, observed)):
            continue
        floor = repost if recent_repost else source_time_floor(source_fact)
        floor_basis = "source_repost" if recent_repost else "source_relative_lower_bound"
        if not recent_repost and basis in OFFICIAL_BASES and published:
            floor = published
            floor_basis = basis
        elif not floor and bounds and since <= bounds[0] <= until:
            floor = bounds[0]
            floor_basis = "official_date_only_lower_bound"
        if not floor or not since <= floor <= min(until, observed):
            continue
        fingerprint = sha256(json.dumps([task, result], sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()
        return {"job_id": job["job_id"], "source_task_id": task["task_id"], "attempt": task["attempt"],
                "result_id": result["result_id"], "source_key": source, "source_listing_id": source_id,
                "official_req_id": job["official_req_id"], "observed_at": observation["observed_at"],
                "since_at": data["since_at"], "until_at": data["until_at"],
                "publication_floor": floor.isoformat(), "publication_basis": floor_basis,
                "snapshot_sha256": fingerprint}
    return None


def match_dispositions(db, jobs, now):
    """Keep discovered-in-window candidates matchable, never directly preparable."""
    jobs = list(jobs)
    result = workflow_dispositions(db, jobs, now)
    if not workflow_policy_enabled(db, now):
        return result
    availability = official_availability(db, jobs, now)
    for job in jobs:
        key = job["job_id"]
        if result[key] not in {"older_than_24h", "publication_unverified"} or availability[key] != "active":
            continue
        row = db.execute("SELECT value_json FROM settings WHERE key=?", ("discovery_admission:" + key,)).fetchone()
        try:
            record = json.loads(row[0]) if row else None
            if not isinstance(record, dict):
                continue
            accepted = db.execute("""SELECT t.state,t.created_at,a.task_json,a.result_json,a.result_id
                FROM tasks t JOIN task_attempts a ON a.task_id=t.task_id AND a.attempt=t.current_attempt
                WHERE t.task_id=? AND a.attempt=?""", (record.get("source_task_id"), record.get("attempt"))).fetchone()
            if not accepted or accepted["state"] != "succeeded" or accepted["result_id"] != record.get("result_id"):
                continue
            proof = discovery_snapshot_admission(db, json.loads(accepted["task_json"]),
                     json.loads(accepted["result_json"]), job, accepted["created_at"], now)
            # A later, more precise primary date may strengthen the same fixed
            # snapshot. Revalidate it, but do not expire the fact merely because
            # its conservative floor/basis becomes more precise. The immutable
            # task/result and exact source/requisition binding must still match.
            identity = ("job_id", "source_task_id", "attempt", "result_id", "source_key", "source_listing_id",
                        "official_req_id", "observed_at", "since_at", "until_at", "snapshot_sha256")
            if proof and all(proof[k] == record.get(k) for k in identity):
                result[key] = "active"
        except (TypeError, ValueError, KeyError):
            continue
    return result


def apply_rechecks(db, files):
    records = {}
    for file in files:
        for row in json.loads(Path(file).read_text(encoding="utf-8")):
            job_id = row["job_id"]
            if job_id in records:
                raise ValueError(f"Duplicate recheck for {job_id}")
            stamp = utc_timestamp(row["observed_at"])
            basis = row.get("published_at_basis")
            posted = row.get("published_at")
            url = urlsplit(row["evidence_url"])
            if (not stamp or row["state"] not in {"open", "closed", "unknown"} or
                    url.scheme != "https" or not url.hostname):
                raise ValueError(f"Invalid verification evidence: {job_id}")
            if (posted or basis) and (basis not in OFFICIAL_BASES or
                    not (date_bounds(posted) if basis == "official_date_only" else utc_timestamp(posted))):
                raise ValueError(f"Invalid publication evidence: {job_id}")
            if row.get("listed_by_at"):
                older = utc_timestamp(row["listed_by_at"])
                historical_url = urlsplit(row.get("listed_by_url") or "")
                if not older or older > stamp or historical_url.scheme != "https" or not historical_url.hostname:
                    raise ValueError(f"Invalid historical listing evidence: {job_id}")
            records[job_id] = row
    db.execute("BEGIN IMMEDIATE")
    try:
        jobs = {r["job_id"]: dict(r) for r in db.execute("SELECT * FROM jobs")}
        if records.keys() != jobs.keys():
            raise ValueError(f"Incomplete coverage. Missing {sorted(jobs.keys() - records.keys())}. "
                             f"Unexpected {sorted(records.keys() - jobs.keys())}.")
        db.execute("""CREATE TABLE IF NOT EXISTS job_freshness_rechecks (
            job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
            observed_at TEXT NOT NULL,
            live_state TEXT NOT NULL CHECK (live_state IN ('open','closed','unknown')),
            evidence_url TEXT NOT NULL, note TEXT NOT NULL,
            listed_by_at TEXT, listed_by_url TEXT
        ) STRICT""")
        for key, row in records.items():
            stamp = utc_timestamp(row["observed_at"])
            normalized = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            db.execute("""INSERT INTO job_freshness_rechecks VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET observed_at=excluded.observed_at, live_state=excluded.live_state,
                    evidence_url=excluded.evidence_url, note=excluded.note,
                    listed_by_at=COALESCE(excluded.listed_by_at,job_freshness_rechecks.listed_by_at),
                    listed_by_url=COALESCE(excluded.listed_by_url,job_freshness_rechecks.listed_by_url)
                WHERE excluded.observed_at>=job_freshness_rechecks.observed_at""",
                (key, normalized, row["state"], row["evidence_url"], row.get("note") or "",
                 row.get("listed_by_at"), row.get("listed_by_url")))
            old = jobs[key]
            verified = utc_timestamp(old["last_verified_at"])
            if row["state"] == "unknown" or (verified and verified > stamp):
                continue
            posted, basis = row.get("published_at"), row.get("published_at_basis")
            if posted and basis != "official_date_only":
                posted = utc_timestamp(posted).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not posted or (old["published_at_basis"] == "official_first_published" and basis != "official_first_published"):
                posted, basis = old["published_at"], old["published_at_basis"]
            db.execute("""UPDATE jobs SET listing_state=?, last_verified_at=?, published_at=?,
                published_at_basis=?, row_version=row_version+1 WHERE job_id=?""",
                (row["state"], normalized, posted, basis, key))
        results = dispositions(db, (dict(r) for r in db.execute("SELECT * FROM jobs")), datetime.now(timezone.utc))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return Counter(results.values()), Counter(r["state"] for r in records.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verification_json", nargs="+")
    args = parser.parse_args()
    with sqlite3.connect(ROOT / "state/jobapply.sqlite", timeout=15) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        reasons, states = apply_rechecks(db, args.verification_json)
    print(json.dumps({"official_status": states, "active_view": reasons}, ensure_ascii=False))


if __name__ == "__main__":
    main()
