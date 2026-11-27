"""Record manually supplied job leads for the normal JobApply discovery pipeline."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobapply_ledger import ROOT, ContractError, Ledger, digest, json_text, time_utc


LEAD_FIELDS = """lead_id company title location observed_url official_req_id note status
                 job_id first_seen_at last_seen_at""".split()
TRACKERS = {"fbclid", "gclid", "gh_src", "msclkid", "trk"}


def plain(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError("人工线索字段须使用文本。")
    return value.strip() or None


def text_key(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def url_key(value):
    """Remove presentation tracking without discarding job-identifying parameters."""
    try:
        fields = urlsplit(value)
        host = fields.hostname
        port = fields.port
    except ValueError as exc:
        raise ContractError("岗位链接格式无效。") from exc
    if (fields.scheme != "https" or not host or fields.username is not None or
            fields.password is not None or any(c.isspace() for c in host)):
        raise ContractError("请提供有效的 HTTPS 岗位链接。")
    host = host.lower().rstrip(".")
    authority = f"[{host}]" if ":" in host else host
    if port not in (None, 443):
        authority += f":{port}"
    path = fields.path or "/"
    fragment = fields.fragment
    # These two Greenhouse hosts identify the same posting on the same board.
    if port in (None, 443) and host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        greenhouse = re.fullmatch(r"/([\w-]+)/jobs/(\d+)(?:/apply)?/?", path)
        if greenhouse:
            authority = "job-boards.greenhouse.io"
            path = "/".join(("", greenhouse[1], "jobs", greenhouse[2]))
            fragment = ""
    query = [
        (name, val) for name, val in parse_qsl(fields.query, keep_blank_values=True)
        if name.lower() not in TRACKERS and not name.lower().startswith("utm_")
    ]
    return urlunsplit(("https", authority, path, urlencode(query), fragment))


class Intake:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.db = ledger.db
        with self.ledger.transaction():
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS manual_job_leads (
                    lead_id TEXT PRIMARY KEY,
                    company TEXT, title TEXT, location TEXT, observed_url TEXT,
                    official_req_id TEXT, note TEXT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'verified')),
                    job_id TEXT REFERENCES jobs(job_id),
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    normalized_url TEXT UNIQUE, company_req_key TEXT UNIQUE,
                    provisional_key TEXT UNIQUE,
                    CHECK ((status = 'verified') = (job_id IS NOT NULL))
                ) STRICT
            """)
            self.db.execute("CREATE INDEX IF NOT EXISTS manual_leads_job ON manual_job_leads(job_id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS manual_leads_status ON manual_job_leads(status, first_seen_at)")

    @staticmethod
    def _public(row):
        return {field: row[field] for field in LEAD_FIELDS}

    def _lead(self, lead_id):
        return self.db.execute("SELECT * FROM manual_job_leads WHERE lead_id=?", (lead_id,)).fetchone()

    def _official_matches(self, company, req, normalized_url):
        """Only already verified official identities are candidates for automatic linkage."""
        matches = {}
        rows = self.db.execute("""
            SELECT j.*, c.display_name FROM jobs j JOIN companies c USING (company_key)
            WHERE j.listing_state IN ('open', 'closed') AND j.last_verified_at IS NOT NULL
        """)
        for row in rows:
            aliases = {text_key(row[name]) for name in ("company_key", "display_name")}
            same_req = req and req == row["official_req_id"] and text_key(company) in aliases
            same_url = False
            if normalized_url:
                for link in (row["official_url"], row["apply_url"]):
                    if not link:
                        continue
                    try:
                        candidate_url = url_key(link)
                    except ContractError:
                        continue
                    if normalized_url == candidate_url:
                        same_url = True
                        break
            if same_req or same_url:
                matches[row["job_id"]] = row
        return matches

    def add(self, *, company=None, title=None, location=None, url=None, req_id=None, note=None):
        company, title, location, url, req_id, note = map(plain, (company, title, location, url, req_id, note))
        if not (url or (company and (title or req_id))):
            raise ContractError("请提供岗位链接，或填写公司与岗位名或 requisition ID。")
        normalized_url = url_key(url) if url else None
        req_key = json_text([text_key(company), req_id]) if company and req_id else None
        provisional = json_text([text_key(company), text_key(title), text_key(location)]) if not (normalized_url or req_key) else None
        with self.ledger.transaction():
            matches = self._official_matches(company, req_id, normalized_url)
            target = next(iter(matches.values())) if len(matches) == 1 else None
            if target and req_id and req_id != target["official_req_id"]:
                raise ContractError("所给 requisition ID 与链接对应的已核实岗位不同。")
            if target and company and text_key(company) not in {text_key(target["company_key"]), text_key(target["display_name"])}:
                raise ContractError("所给公司与链接对应的已核实岗位不同。")
            if len(matches) > 1:
                raise ContractError("链接或编号匹配到多个已核实岗位，请核对后再入库。")
            job_id = target["job_id"] if target else None
            matches = self.db.execute("""
                SELECT * FROM manual_job_leads
                WHERE normalized_url = ? OR company_req_key = ? OR provisional_key = ?
                ORDER BY first_seen_at, lead_id
            """, (normalized_url, req_key, provisional)).fetchall()
            if len(matches) > 1:
                raise ContractError("链接与编号分别对应不同的人工线索，请先核对。")
            existing = matches[0] if matches else None
            if not existing and job_id:
                existing = self.db.execute("""
                    SELECT * FROM manual_job_leads WHERE job_id=? ORDER BY first_seen_at, lead_id LIMIT 1
                """, (job_id,)).fetchone()
            now = time_utc()
            if existing:
                if job_id and existing["job_id"] not in (None, job_id):
                    raise ContractError("这条线索已关联另一条官方岗位。")
                if req_id and existing["official_req_id"] not in (None, req_id):
                    raise ContractError("同一线索的 requisition ID 不一致，请核对。")
                if note and existing["note"]:
                    note = existing["note"] if note in existing["note"].split("\n\n") else existing["note"] + "\n\n" + note
                self.db.execute("""
                    UPDATE manual_job_leads
                    SET company=coalesce(company, ?), title=coalesce(title, ?), location=coalesce(location, ?),
                        observed_url=coalesce(observed_url, ?), official_req_id=coalesce(official_req_id, ?),
                        normalized_url=coalesce(normalized_url, ?), company_req_key=coalesce(company_req_key, ?),
                        note=coalesce(?, note), job_id=coalesce(job_id, ?),
                        status=CASE WHEN coalesce(job_id, ?) IS NULL THEN 'pending' ELSE 'verified' END,
                        last_seen_at=?
                    WHERE lead_id=?
                """, (company, title, location, url, req_id, normalized_url, req_key, note, job_id, job_id, now, existing["lead_id"]))
                return {"outcome": "verified" if job_id and not existing["job_id"] else "duplicate",
                        "lead": self._public(self._lead(existing["lead_id"]))}
            if target:
                company = company or target["display_name"]
                title = title or target["title"]
                location = location or target["location"]
                req_id = req_id or target["official_req_id"]
            lead_id = "ml_" + digest(json_text([normalized_url, req_key, provisional]))[:24]
            self.db.execute("""
                INSERT INTO manual_job_leads
                (lead_id, company, title, location, observed_url, official_req_id, note, status,
                 job_id, first_seen_at, last_seen_at, normalized_url, company_req_key, provisional_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (lead_id, company, title, location, url, req_id, note,
                  "verified" if job_id else "pending", job_id, now, now, normalized_url, req_key, provisional))
            return {"outcome": "verified" if job_id else "added", "lead": self._public(self._lead(lead_id))}

    def pending(self):
        return [self._public(row) for row in self.db.execute("""
            SELECT * FROM manual_job_leads WHERE status='pending' ORDER BY first_seen_at, lead_id
        """)]

    def resolve(self, lead_id, job_id):
        with self.ledger.transaction():
            lead = self._lead(lead_id)
            if not lead:
                raise ContractError("找不到该人工线索。")
            target = self.db.execute("""
                SELECT j.*, c.display_name FROM jobs j JOIN companies c USING (company_key)
                WHERE j.job_id=? AND j.last_verified_at IS NOT NULL AND j.listing_state IN ('open', 'closed')
            """, (job_id,)).fetchone()
            if not target:
                raise ContractError("请先通过 discover 核实并录入官方岗位。")
            if lead["job_id"] not in (None, job_id):
                raise ContractError("这条线索已关联另一条官方岗位。")
            matches = self._official_matches(lead["company"], lead["official_req_id"], lead["normalized_url"])
            if set(matches) - {job_id}:
                raise ContractError("线索的链接或编号指向另一条已核实岗位，请核对。")
            if lead["job_id"] == job_id:
                return {"outcome": "duplicate", "lead": self._public(lead)}
            another = self.db.execute("SELECT 1 FROM manual_job_leads WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
            self.db.execute("""
                UPDATE manual_job_leads SET status='verified', job_id=?,
                    company=coalesce(company, ?), title=coalesce(title, ?),
                    location=coalesce(location, ?), official_req_id=coalesce(official_req_id, ?), last_seen_at=?
                WHERE lead_id=?
            """, (job_id, target["display_name"], target["title"], target["location"],
                  target["official_req_id"], time_utc(), lead_id))
            return {"outcome": "duplicate_job" if another else "verified", "lead": self._public(self._lead(lead_id))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Private workspace root")
    parser.add_argument("--db", help="Alternate SQLite path for an isolated workspace.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Ensure the additive lead table exists.")
    commands.add_parser("pending", help="List unverified manual leads for discover.")
    add = commands.add_parser("add", help="Record a link, or a company and role or requisition ID.")
    for field in ("url", "company", "title", "location", "req-id", "note"):
        add.add_argument(f"--{field}")
    resolve = commands.add_parser("resolve", help="Link a lead after it has been verified through discover.")
    resolve.add_argument("lead_id")
    resolve.add_argument("--job-id", required=True)
    args = parser.parse_args()
    ledger = None
    try:
        ledger = Ledger(root=args.root, db_path=args.db)
        intake = Intake(ledger)
        if args.command == "add":
            result = intake.add(company=args.company, title=args.title, location=args.location,
                                url=args.url, req_id=args.req_id, note=args.note)
        elif args.command == "pending":
            result = intake.pending()
        elif args.command == "resolve":
            result = intake.resolve(args.lead_id, args.job_id)
        else:
            result = {"outcome": "initialized"}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ContractError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if ledger:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
