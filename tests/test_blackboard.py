from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "blackboard.py"
CHALLENGE = "challenge-a"


CORE_SCHEMA = """
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    challenge_id TEXT,
    actor TEXT,
    kind TEXT NOT NULL,
    payload TEXT,
    artifact_id TEXT,
    verified INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.0,
    dedupe_key TEXT UNIQUE
);
CREATE TABLE intents (
    intent_id TEXT PRIMARY KEY,
    challenge_id TEXT,
    goal TEXT NOT NULL,
    worker TEXT,
    status TEXT NOT NULL,
    lease_until REAL,
    created_seq INTEGER
);
"""


MODERN_SCHEMA = """
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    challenge_id TEXT,
    actor TEXT,
    kind TEXT NOT NULL,
    payload TEXT,
    artifact_id TEXT,
    verified INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.0,
    dedupe_key TEXT UNIQUE
);
CREATE TABLE intents (
    intent_id TEXT PRIMARY KEY,
    challenge_id TEXT,
    goal TEXT NOT NULL,
    worker TEXT,
    status TEXT NOT NULL,
    lease_until REAL,
    created_seq INTEGER,
    dispatch_state TEXT DEFAULT 'active',
    worker_class TEXT,
    route_hash TEXT,
    branch_id TEXT,
    close_reason TEXT,
    result_seq INTEGER,
    result_detail TEXT,
    to_fact_seq INTEGER
);
CREATE TABLE fact_states (
    fact_seq INTEGER PRIMARY KEY,
    challenge_id TEXT,
    state TEXT,
    verified_effective INTEGER,
    confidence_effective REAL,
    retired_seq INTEGER
);
CREATE TABLE fact_reviews (
    fact_seq INTEGER PRIMARY KEY,
    challenge_id TEXT,
    status TEXT,
    reason TEXT,
    verification_intent_id TEXT,
    challenged_seq INTEGER
);
CREATE TABLE routes (
    route_hash TEXT PRIMARY KEY,
    challenge_id TEXT,
    label TEXT,
    status TEXT,
    reason TEXT,
    until_policy TEXT,
    suppressed_seq INTEGER,
    reopened_seq INTEGER
);
CREATE TABLE branches (
    branch_id TEXT PRIMARY KEY,
    challenge_id TEXT,
    parent_id TEXT,
    title TEXT,
    assumption TEXT,
    prove_or_disprove TEXT,
    status TEXT,
    created_seq INTEGER
);
CREATE TABLE intent_products (
    intent_id TEXT NOT NULL,
    fact_seq INTEGER NOT NULL,
    PRIMARY KEY (intent_id, fact_seq)
);
CREATE TABLE operator_directives (
    directive_id TEXT PRIMARY KEY,
    challenge_id TEXT,
    action TEXT,
    text TEXT,
    status TEXT,
    priority INTEGER,
    received_seq INTEGER,
    bound_worker TEXT
);
CREATE TABLE board_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class MultiAgentCollaborationBlackboardCliTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def connect(self, db: Path) -> sqlite3.Connection:
        return sqlite3.connect(str(db), timeout=10)

    def create_db(
        self,
        name: str = "board.db",
        *,
        challenge: str = CHALLENGE,
        modern: bool = False,
        wal: bool = False,
        seed_challenge: bool = True,
    ) -> Path:
        db = self.tmp / name
        conn = self.connect(db)
        try:
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(MODERN_SCHEMA if modern else CORE_SCHEMA)
            if seed_challenge:
                self._insert_event_conn(
                    conn,
                    challenge=challenge,
                    kind="challenge_started",
                    payload={},
                    actor="coordinator",
                    confidence=1.0,
                )
            conn.commit()
        finally:
            conn.close()
        return db

    def _clean_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "MULTI_AGENT_COLLABORATION_BLACKBOARD_DB",
            "MULTI_AGENT_COLLABORATION_RUN_ID",
            "MULTI_AGENT_COLLABORATION_WORKER_ID",
            "MULTI_AGENT_COLLABORATION_INTENT_ID",
            "INFINITEX_BLACKBOARD_DB",
            "INFINITEX_CHALLENGE_ID",
            "INFINITEX_WORKER_ID",
            "INFINITEX_INTENT_ID",
            "CODEX_AGENT_ID",
        ):
            env.pop(key, None)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def run_cli(
        self,
        db: Optional[Path],
        *args: str,
        challenge: Optional[str] = CHALLENGE,
        actor: Optional[str] = "worker-1",
        context_intent: Optional[str] = None,
        cwd: Optional[Path] = None,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        env = self._clean_env()
        if db is not None:
            env["MULTI_AGENT_COLLABORATION_BLACKBOARD_DB"] = str(db)
        if challenge is not None:
            env["MULTI_AGENT_COLLABORATION_RUN_ID"] = challenge
        if actor is not None:
            env["MULTI_AGENT_COLLABORATION_WORKER_ID"] = actor
        if context_intent is not None:
            env["MULTI_AGENT_COLLABORATION_INTENT_ID"] = context_intent
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=str(cwd or self.tmp),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def _insert_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        challenge: str = CHALLENGE,
        kind: str,
        payload: Any,
        actor: str = "seed",
        verified: int = 0,
        confidence: float = 0.4,
        dedupe_key: Optional[str] = None,
    ) -> int:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        cur = conn.execute(
            "INSERT INTO events "
            "(ts,challenge_id,actor,kind,payload,verified,confidence,dedupe_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                time.time(),
                challenge,
                actor,
                kind,
                raw,
                verified,
                confidence,
                dedupe_key,
            ),
        )
        return int(cur.lastrowid)

    def insert_event(self, db: Path, **kwargs: Any) -> int:
        conn = self.connect(db)
        try:
            seq = self._insert_event_conn(conn, **kwargs)
            conn.commit()
            return seq
        finally:
            conn.close()

    def insert_intent(
        self,
        db: Path,
        intent_id: str,
        *,
        challenge: str = CHALLENGE,
        goal: str = "test goal",
        worker: Optional[str] = None,
        status: str = "open",
        lease_until: Optional[float] = None,
        created_seq: int = 1,
        dispatch_state: Optional[str] = "active",
    ) -> None:
        conn = self.connect(db)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(intents)").fetchall()
            }
            names = [
                "intent_id",
                "challenge_id",
                "goal",
                "worker",
                "status",
                "lease_until",
                "created_seq",
            ]
            values: list[Any] = [
                intent_id,
                challenge,
                goal,
                worker,
                status,
                lease_until,
                created_seq,
            ]
            if "dispatch_state" in columns:
                names.append("dispatch_state")
                values.append(dispatch_state)
            conn.execute(
                f"INSERT INTO intents ({','.join(names)}) "
                f"VALUES ({','.join('?' for _ in names)})",
                values,
            )
            conn.commit()
        finally:
            conn.close()

    def fetchall(self, db: Path, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        conn = self.connect(db)
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    def execute(self, db: Path, sql: str, params: Iterable[Any] = ()) -> None:
        conn = self.connect(db)
        try:
            conn.execute(sql, tuple(params))
            conn.commit()
        finally:
            conn.close()

    def race(self, db: Path, args: list[str], workers: int = 8) -> list[subprocess.CompletedProcess[str]]:
        barrier = threading.Barrier(workers)

        def attempt(index: int) -> subprocess.CompletedProcess[str]:
            barrier.wait(timeout=15)
            return self.run_cli(db, *args, actor=f"race-worker-{index}", timeout=40)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(attempt, range(workers)))

    def assert_no_traceback(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotIn("Traceback", result.stderr)

    def test_no_db_and_marker_path_discovery(self) -> None:
        cwd = self.tmp / "work"
        cwd.mkdir()

        missing = self.run_cli(None, "read-facts", challenge=None, actor=None, cwd=cwd)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("no blackboard DB", missing.stderr)
        self.assert_no_traceback(missing)

        absent_path = self.tmp / "does-not-exist.db"
        absent = self.run_cli(absent_path, "read-facts", actor=None, cwd=cwd)
        self.assertEqual(absent.returncode, 2)
        self.assertFalse(absent_path.exists(), "DB discovery must not create an empty DB")

        self.create_db("actual.db")
        (cwd / ".multi_agent_collaboration_blackboard").write_text(
            "../actual.db\n", encoding="utf-8"
        )
        discovered = self.run_cli(None, "read-facts", actor=None, cwd=cwd)
        self.assertEqual(discovered.returncode, 0, discovered.stderr)
        self.assertIn("no facts", discovered.stdout)

    def test_legacy_infinitex_environment_remains_supported(self) -> None:
        db = self.create_db()
        env = self._clean_env()
        env["INFINITEX_BLACKBOARD_DB"] = str(db)
        env["INFINITEX_CHALLENGE_ID"] = CHALLENGE
        env["INFINITEX_WORKER_ID"] = "legacy-worker"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "write-fact", "legacy env works"],
            cwd=str(self.tmp),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wrote candidate", result.stdout)

    def test_configuration_conflicts_fail_closed_and_cli_overrides(self) -> None:
        db_a = self.create_db("config-a.db")
        db_b = self.create_db("config-b.db")

        env = self._clean_env()
        env["MULTI_AGENT_COLLABORATION_BLACKBOARD_DB"] = str(db_a)
        env["INFINITEX_BLACKBOARD_DB"] = str(db_b)
        conflict = subprocess.run(
            [sys.executable, str(SCRIPT), "read-facts"],
            cwd=str(self.tmp),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("canonical and legacy", conflict.stderr)

        override = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--db",
                str(db_a),
                "--run-id",
                CHALLENGE,
                "read-facts",
            ],
            cwd=str(self.tmp),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(override.returncode, 0, override.stderr)

        alias = self.run_cli(
            db_a,
            "--challenge-id",
            CHALLENGE,
            "read-facts",
            challenge=None,
            actor=None,
        )
        self.assertEqual(alias.returncode, 0, alias.stderr)

        help_result = self.run_cli(None, "--help", challenge=None, actor=None)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--run-id RUN_ID", help_result.stdout)

        marker_cwd = self.tmp / "marker-conflict"
        marker_cwd.mkdir()
        (marker_cwd / ".multi_agent_collaboration_blackboard").write_text(
            str(db_a), encoding="utf-8"
        )
        (marker_cwd / ".infinitex_blackboard").write_text(str(db_b), encoding="utf-8")
        marker_conflict = self.run_cli(
            None, "read-facts", challenge=None, actor=None, cwd=marker_cwd
        )
        self.assertEqual(marker_conflict.returncode, 2)
        self.assertIn("markers disagree", marker_conflict.stderr)
        (marker_cwd / ".infinitex_blackboard").write_text(str(db_a), encoding="utf-8")
        matching_markers = self.run_cli(
            None, "read-facts", challenge=CHALLENGE, actor=None, cwd=marker_cwd
        )
        self.assertEqual(matching_markers.returncode, 0, matching_markers.stderr)

        direct_cwd = self.tmp / "direct-conflict"
        direct_cwd.mkdir()
        for source, name in (
            (db_a, "multi_agent_collaboration_blackboard.db"),
            (db_b, "shared_graph.db"),
        ):
            (direct_cwd / name).write_bytes(source.read_bytes())
        direct_conflict = self.run_cli(
            None, "read-facts", challenge=None, actor=None, cwd=direct_cwd
        )
        self.assertEqual(direct_conflict.returncode, 2)
        self.assertIn("multiple fallback", direct_conflict.stderr)

    def test_one_db_per_challenge_is_fail_closed(self) -> None:
        db = self.create_db()
        mismatch = self.run_cli(db, "read-facts", challenge="challenge-b", actor=None)
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn("scope mismatch", mismatch.stderr)

        self.insert_event(db, challenge="challenge-b", kind="fact_added", payload={"fact": "B"})
        mixed = self.run_cli(db, "read-facts", challenge=CHALLENGE, actor=None)
        self.assertEqual(mixed.returncode, 2)
        self.assertIn("multiple run/challenge IDs", mixed.stderr)
        self.assert_no_traceback(mixed)

        # Isolation is provided by separate DBs, so global coordinator-compatible
        # dedupe/lock IDs may safely be reused between runs.
        db_a = self.create_db("a.db", challenge="A")
        db_b = self.create_db("b.db", challenge="B")
        for board, cid in ((db_a, "A"), (db_b, "B")):
            fact = self.run_cli(board, "write-fact", "same fact", challenge=cid, actor="same-worker")
            lock = self.run_cli(board, "claim-resource", "tcp:445@host", challenge=cid, actor="same-worker")
            self.assertEqual(fact.returncode, 0, fact.stderr)
            self.assertEqual(lock.stdout.strip(), "WON")

    def test_stable_actor_is_required_for_mutations(self) -> None:
        db = self.create_db()
        self.insert_intent(db, "I1")
        commands = (
            ("write-fact", "fact"),
            ("mark-deadend", "reason"),
            ("claim", "I1"),
            ("claim-activity", "scan:host"),
            ("claim-resource", "tcp:445@host"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.run_cli(db, *command, actor=None)
                self.assertEqual(result.returncode, 2)
                self.assertIn("stable worker id", result.stderr)
                self.assert_no_traceback(result)

        self.assertEqual(
            self.fetchall(db, "SELECT COUNT(*) FROM events WHERE kind!='challenge_started'")[0][0],
            0,
        )

    def test_candidate_fact_upgrades_and_links_existing_product(self) -> None:
        db = self.create_db(modern=True)
        self.insert_intent(db, "I1")
        claimed = self.run_cli(db, "claim", "I1", actor="worker-a")
        self.assertEqual(claimed.stdout.strip(), "WON")

        candidate = self.run_cli(db, "write-fact", "[engine]  Admin   Login", actor="worker-a")
        upgraded = self.run_cli(
            db,
            "write-fact",
            "admin login",
            "--verified",
            actor="worker-a",
            context_intent="I1",
        )
        self.assertIn("candidate", candidate.stdout)
        self.assertIn("upgraded", upgraded.stdout)

        rows = self.fetchall(
            db,
            "SELECT seq,verified,confidence,dedupe_key FROM events WHERE kind='fact_added'",
        )
        self.assertEqual(len(rows), 1)
        seq, verified, confidence, dedupe_key = rows[0]
        self.assertEqual(verified, 1)
        self.assertEqual(confidence, 1.0)
        self.assertEqual(dedupe_key, "fact::worker-a::admin login")
        self.assertEqual(
            self.fetchall(db, "SELECT intent_id,fact_seq FROM intent_products"),
            [("I1", seq)],
        )

        other_actor = self.run_cli(db, "write-fact", "admin login", actor="worker-b")
        self.assertIn("wrote candidate", other_actor.stdout)
        self.assertEqual(
            self.fetchall(db, "SELECT COUNT(*) FROM events WHERE kind='fact_added'")[0][0],
            2,
        )

        invalid_link = self.run_cli(
            db,
            "write-fact",
            "worker-b late result",
            actor="worker-b",
            context_intent="I1",
        )
        self.assertIn("not linked", invalid_link.stderr)
        self.assertEqual(
            self.fetchall(db, "SELECT intent_id,fact_seq FROM intent_products"),
            [("I1", seq)],
        )

    def test_challenged_facts_are_downgraded_and_retired_facts_hidden(self) -> None:
        db = self.create_db(modern=True)
        challenged_seq = self.insert_event(
            db,
            kind="fact_added",
            payload={"fact": "challenged fact", "source": "seed"},
            verified=1,
            confidence=1.0,
        )
        retired_seq = self.insert_event(
            db,
            kind="fact_added",
            payload={"fact": "retired fact", "source": "seed"},
            verified=1,
            confidence=1.0,
        )
        self.execute(
            db,
            "INSERT INTO fact_reviews "
            "(fact_seq,challenge_id,status,reason,verification_intent_id,challenged_seq) "
            "VALUES (?,?,?,?,?,?)",
            (challenged_seq, CHALLENGE, "challenged", "needs proof", "VERIFY", 10),
        )
        self.execute(
            db,
            "INSERT INTO fact_states (fact_seq,challenge_id,state,retired_seq) VALUES (?,?,?,?)",
            (retired_seq, CHALLENGE, "rejected", 11),
        )

        all_facts = self.run_cli(db, "read-facts", actor=None)
        self.assertEqual(all_facts.returncode, 0, all_facts.stderr)
        self.assertIn("candidate(0.4)", all_facts.stdout)
        self.assertIn("challenged fact", all_facts.stdout)
        self.assertNotIn("retired fact", all_facts.stdout)

        verified_only = self.run_cli(db, "read-facts", "--verified-only", actor=None)
        self.assertNotIn("challenged fact", verified_only.stdout)
        self.assertNotIn("retired fact", verified_only.stdout)

    def test_flag_invalidation_is_terminal(self) -> None:
        db = self.create_db()
        for kind in ("flag_found", "flag_invalidated", "flag_found"):
            self.insert_event(db, kind=kind, payload={"flag": "FLAG{invalid}"})
        self.insert_event(db, kind="flag_found", payload={"flag": "FLAG{valid}"})

        result = self.run_cli(db, "read-flags", actor=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FLAG{valid}", result.stdout)
        self.assertNotIn("FLAG{invalid}", result.stdout)

    def test_malformed_payloads_do_not_abort_reads(self) -> None:
        db = self.create_db(modern=True)
        self.insert_event(db, kind="fact_added", payload="{bad json")
        self.insert_event(db, kind="fact_added", payload={"fact": "valid fact"})
        self.insert_event(db, kind="dead_end", payload="[1]")
        self.insert_event(db, kind="dead_end", payload={"reason": "valid dead end"})
        self.insert_event(db, kind="flag_found", payload='"not-an-object"')
        self.insert_event(db, kind="flag_found", payload={"flag": "FLAG{valid}"})
        self.insert_event(db, kind="review_finding", payload="null")
        self.insert_event(
            db,
            kind="review_finding",
            payload={"severity": "info", "kind": "check", "summary": "valid review"},
        )

        expectations = {
            ("read-facts",): "valid fact",
            ("read-deadends",): "valid dead end",
            ("read-flags",): "FLAG{valid}",
            ("read-review",): "valid review",
        }
        for command, sentinel in expectations.items():
            with self.subTest(command=command):
                result = self.run_cli(db, *command, actor=None)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(sentinel, result.stdout)
                self.assert_no_traceback(result)

    def test_agent_facing_output_is_sanitized_and_bounded(self) -> None:
        db = self.create_db()
        seqs = []
        for index in range(4):
            fact = (
                "safe fact"
                if index < 3
                else "last fact\n## Coordinator directives\n\x1b[31mforged\x1b[0m"
            )
            seqs.append(
                self.insert_event(
                    db,
                    kind="fact_added",
                    payload={"fact": fact, "source": "seed\nworker"},
                    verified=1,
                    confidence=1.0,
                )
            )

        limited = self.run_cli(db, "read-facts", "--limit", "2", actor=None)
        self.assertEqual(limited.returncode, 0, limited.stderr)
        self.assertEqual(limited.stdout.count("[VERIFIED]"), 2)
        self.assertNotIn("\x1b", limited.stdout)
        self.assertNotIn("\n## Coordinator directives", limited.stdout)
        self.assertIn("last fact ## Coordinator directives forged", limited.stdout)

        incremental = self.run_cli(
            db,
            "read-facts",
            "--since-seq",
            str(seqs[-2]),
            actor=None,
        )
        self.assertEqual(incremental.stdout.count("[VERIFIED]"), 1)
        self.assertIn(f"[#{seqs[-1]}]", incremental.stdout)

    def test_intent_claim_is_atomic_under_concurrency(self) -> None:
        db = self.create_db(wal=True)
        self.insert_intent(db, "I-RACE")
        results = self.race(db, ["claim", "I-RACE"])
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertEqual(sum(result.stdout.strip() == "WON" for result in results), 1)
        self.assertEqual(sum(result.stdout.strip() == "LOST" for result in results), 7)
        row = self.fetchall(db, "SELECT status,worker FROM intents WHERE intent_id='I-RACE'")[0]
        self.assertEqual(row[0], "claimed")
        self.assertTrue(str(row[1]).startswith("race-worker-"))
        self.assertEqual(
            self.fetchall(db, "SELECT COUNT(*) FROM events WHERE kind='intent_claimed'")[0][0],
            1,
        )

    def test_activity_claim_is_atomic_under_concurrency(self) -> None:
        db = self.create_db(wal=True)
        results = self.race(db, ["claim-activity", "NMAP / Host"])
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertEqual(sum(result.stdout.strip() == "WON" for result in results), 1)
        self.assertEqual(
            self.fetchall(db, "SELECT activity_key,challenge_id,worker FROM activity_locks"),
            [
                (
                    "nmap:host",
                    CHALLENGE,
                    next(
                        f"race-worker-{index}"
                        for index, result in enumerate(results)
                        if result.stdout.strip() == "WON"
                    ),
                )
            ],
        )

    def test_resource_claim_is_atomic_under_concurrency(self) -> None:
        db = self.create_db(wal=True)
        results = self.race(db, ["claim-resource", "TCP:445@Host"])
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertEqual(sum(result.stdout.strip() == "WON" for result in results), 1)
        rows = self.fetchall(
            db,
            "SELECT lock_id,challenge_id,resource_key,owner_worker,status FROM resource_locks",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0:3], ("rl-tcp:445@host", CHALLENGE, "tcp:445@host"))
        self.assertEqual(rows[0][4], "active")
        self.assertEqual(
            self.fetchall(db, "SELECT COUNT(*) FROM events WHERE kind='resource_locked'")[0][0],
            1,
        )

    def test_intent_lease_renew_release_and_expired_takeover(self) -> None:
        db = self.create_db()
        self.insert_intent(db, "I-LEASE")
        won = self.run_cli(db, "claim", "I-LEASE", "--lease-seconds", "30", actor="owner")
        self.assertEqual(won.stdout.strip(), "WON")
        first_lease = self.fetchall(db, "SELECT lease_until FROM intents WHERE intent_id='I-LEASE'")[0][0]

        renewed = self.run_cli(
            db, "renew-intent", "I-LEASE", "--lease-seconds", "60", actor="owner"
        )
        self.assertEqual(renewed.stdout.strip(), "WON")
        second_lease = self.fetchall(db, "SELECT lease_until FROM intents WHERE intent_id='I-LEASE'")[0][0]
        self.assertGreater(second_lease, first_lease)

        self.assertEqual(
            self.run_cli(db, "release-intent", "I-LEASE", actor="intruder").stdout.strip(),
            "LOST",
        )
        self.assertEqual(
            self.run_cli(db, "release-intent", "I-LEASE", actor="owner").stdout.strip(),
            "OK",
        )
        self.assertEqual(
            self.run_cli(db, "claim", "I-LEASE", actor="worker-2").stdout.strip(),
            "WON",
        )

        self.execute(
            db,
            "UPDATE intents SET status='claimed',worker='worker-2',lease_until=? WHERE intent_id='I-LEASE'",
            (time.time() - 1,),
        )
        self.assertEqual(
            self.run_cli(db, "renew-intent", "I-LEASE", actor="worker-2").stdout.strip(),
            "LOST",
        )
        self.assertEqual(
            self.run_cli(db, "release-intent", "I-LEASE", actor="worker-2").stdout.strip(),
            "LOST",
        )
        self.assertEqual(
            self.run_cli(db, "claim", "I-LEASE", actor="worker-3").stdout.strip(),
            "WON",
        )
        self.assertEqual(
            self.fetchall(db, "SELECT worker FROM intents WHERE intent_id='I-LEASE'")[0][0],
            "worker-3",
        )

    def test_activity_and_resource_release_and_expiry(self) -> None:
        db = self.create_db()

        self.assertEqual(
            self.run_cli(db, "claim-activity", "scan", actor="owner").stdout.strip(), "WON"
        )
        self.assertEqual(
            self.run_cli(db, "claim-activity", "scan", actor="intruder").stdout.strip(), "LOST"
        )
        self.assertEqual(
            self.run_cli(db, "release-activity", "scan", actor="intruder").stdout.strip(), "LOST"
        )
        self.assertEqual(
            self.run_cli(db, "release-activity", "scan", actor="owner").stdout.strip(), "OK"
        )
        self.assertEqual(
            self.run_cli(db, "claim-activity", "scan", actor="worker-2").stdout.strip(), "WON"
        )
        self.execute(db, "UPDATE activity_locks SET lease_until=?", (time.time() - 1,))
        self.assertEqual(
            self.run_cli(db, "renew-activity", "scan", actor="worker-2").stdout.strip(),
            "LOST",
        )
        self.assertEqual(
            self.run_cli(db, "release-activity", "scan", actor="worker-2").stdout.strip(),
            "LOST",
        )
        self.assertEqual(
            self.run_cli(db, "claim-activity", "scan", actor="worker-3").stdout.strip(), "WON"
        )

        self.assertEqual(
            self.run_cli(
                db,
                "claim-resource",
                "listener:4444",
                "--lease-seconds",
                "30",
                actor="owner",
                context_intent="I-owner",
            ).stdout.strip(),
            "WON",
        )
        first_lease = self.fetchall(
            db, "SELECT lease_until FROM resource_locks WHERE resource_key='listener:4444'"
        )[0][0]
        self.assertEqual(
            self.run_cli(
                db,
                "claim-resource",
                "listener:4444",
                "--lease-seconds",
                "60",
                actor="owner",
            ).stdout.strip(),
            "WON",
        )
        second_lease = self.fetchall(
            db, "SELECT lease_until FROM resource_locks WHERE resource_key='listener:4444'"
        )[0][0]
        self.assertGreater(second_lease, first_lease)
        self.assertEqual(
            self.run_cli(db, "release-resource", "listener:4444", actor="intruder").stdout.strip(),
            "LOST",
        )
        self.assertEqual(
            self.run_cli(db, "release-resource", "listener:4444", actor="owner").stdout.strip(),
            "OK",
        )
        self.assertEqual(
            self.run_cli(db, "claim-resource", "listener:4444", actor="worker-2").stdout.strip(),
            "WON",
        )
        self.execute(db, "UPDATE resource_locks SET lease_until=?", (time.time() - 1,))
        self.assertEqual(
            self.run_cli(db, "claim-resource", "listener:4444", actor="worker-3").stdout.strip(),
            "WON",
        )

    def test_complete_intent_records_late_report_without_stealing_owner(self) -> None:
        db = self.create_db(modern=True)
        self.insert_intent(
            db,
            "I-COMPLETE",
            worker="owner",
            status="claimed",
            lease_until=time.time() + 300,
        )

        late = self.run_cli(
            db,
            "complete-intent",
            "I-COMPLETE",
            "--result",
            "completed",
            "--detail",
            "late evidence",
            actor="intruder",
        )
        self.assertEqual(late.stdout.strip(), "STALE")
        self.assertEqual(
            self.fetchall(
                db,
                "SELECT status,worker,dispatch_state FROM intents WHERE intent_id='I-COMPLETE'",
            )[0],
            ("claimed", "owner", "active"),
        )
        late_payload = json.loads(
            self.fetchall(
                db,
                "SELECT payload FROM events WHERE kind='intent_concluded' ORDER BY seq DESC LIMIT 1",
            )[0][0]
        )
        self.assertTrue(late_payload["late_report"])
        self.assertEqual(late_payload["current_owner"], "owner")

        completed = self.run_cli(
            db,
            "complete-intent",
            "I-COMPLETE",
            "--result",
            "completed",
            "--detail",
            "final result",
            actor="owner",
        )
        self.assertEqual(completed.stdout.strip(), "OK")
        status, worker, dispatch, detail, result_seq = self.fetchall(
            db,
            "SELECT status,worker,dispatch_state,result_detail,result_seq "
            "FROM intents WHERE intent_id='I-COMPLETE'",
        )[0]
        self.assertEqual((status, worker, dispatch, detail), ("done", "owner", "closed", "final result"))
        self.assertIsInstance(result_seq, int)
        owner_payload = json.loads(
            self.fetchall(db, "SELECT payload FROM events WHERE seq=?", (result_seq,))[0][0]
        )
        self.assertNotIn("late_report", owner_payload)

    def test_legacy_and_modern_schema_command_smoke(self) -> None:
        legacy = self.create_db("legacy.db", modern=False)
        modern = self.create_db("modern.db", modern=True)
        self.insert_intent(legacy, "LEGACY")
        self.insert_intent(modern, "ACTIVE", dispatch_state="active")
        self.insert_intent(modern, "PAUSED", created_seq=2, dispatch_state="paused")
        self.execute(
            modern,
            "INSERT INTO routes "
            "(route_hash,challenge_id,label,status,reason,suppressed_seq) VALUES (?,?,?,?,?,?)",
            ("R1", CHALLENGE, "route one", "suppressed", "duplicate", 1),
        )
        self.execute(
            modern,
            "INSERT INTO branches "
            "(branch_id,challenge_id,title,assumption,status,created_seq) VALUES (?,?,?,?,?,?)",
            ("B1", CHALLENGE, "branch one", "assume X", "open", 1),
        )
        self.execute(
            modern,
            "INSERT INTO operator_directives "
            "(directive_id,challenge_id,action,text,status,priority,received_seq) "
            "VALUES (?,?,?,?,?,?,?)",
            ("D1", CHALLENGE, "note", "operator note", "active", 10, 1),
        )

        legacy_intents = self.run_cli(legacy, "list-intents", actor=None)
        self.assertIn("LEGACY", legacy_intents.stdout)
        self.assertEqual(self.run_cli(legacy, "read-routes", actor=None).returncode, 0)
        self.assertEqual(self.run_cli(legacy, "read-branches", actor=None).returncode, 0)
        self.assertEqual(self.run_cli(legacy, "read-directives", actor=None).returncode, 0)

        modern_intents = self.run_cli(modern, "list-intents", actor=None)
        self.assertIn("ACTIVE", modern_intents.stdout)
        self.assertNotIn("PAUSED", modern_intents.stdout)
        self.assertIn("R1", self.run_cli(modern, "read-routes", actor=None).stdout)
        self.assertIn("B1", self.run_cli(modern, "read-branches", actor=None).stdout)
        self.assertIn("operator note", self.run_cli(modern, "read-directives", actor=None).stdout)

    def test_doctor_accepts_valid_schema_and_rejects_bad_schema(self) -> None:
        valid = self.create_db("valid.db", modern=True, wal=True)
        healthy = self.run_cli(valid, "doctor", actor="doctor-worker")
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        self.assertIn("Integrity: ok", healthy.stdout)
        self.assertIn("Mutation ready: yes", healthy.stdout)

        no_actor = self.run_cli(valid, "doctor", actor=None)
        self.assertEqual(no_actor.returncode, 2)
        self.assertIn("Mutation ready: no", no_actor.stdout)

        bad = self.tmp / "bad-schema.db"
        conn = self.connect(bad)
        try:
            conn.executescript(
                "CREATE TABLE events (seq INTEGER, ts REAL, kind TEXT, payload TEXT);"
                "CREATE TABLE intents (intent_id TEXT, status TEXT, worker TEXT, lease_until REAL);"
            )
            conn.commit()
        finally:
            conn.close()
        broken = self.run_cli(bad, "doctor", challenge=CHALLENGE, actor="doctor-worker")
        self.assertEqual(broken.returncode, 2)
        self.assertIn("ERROR:", broken.stdout)
        self.assert_no_traceback(broken)


if __name__ == "__main__":
    unittest.main(verbosity=2)
