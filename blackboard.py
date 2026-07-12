#!/usr/bin/env python3
"""Worker CLI for the Multi-agent Collaboration shared SQLite blackboard.

The CLI is intentionally dependency-free.  It is safe to call from many workers:
reads are challenge-scoped, claims use atomic SQLite transactions, and mutations
emit durable events when the board schema supports them.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import Any, Iterator, Optional, Sequence


VERSION = "2.1.0"
SQLITE_MAGIC = b"SQLite format 3\x00"
MARKER_NAMES = (".multi_agent_collaboration_blackboard", ".Alex_blackboard")
DIRECT_DB_NAMES = ("multi_agent_collaboration_blackboard.db", "shared_graph.db")

_CLI_DB = ""
_CLI_CHALLENGE_ID = ""
_CLI_ACTOR = ""
_CLI_INTENT_ID = ""


class BlackboardError(RuntimeError):
    """A configuration or schema problem that should be shown without a traceback."""


def _compatible_env(
    canonical_names: Sequence[str], legacy_names: Sequence[str] = ()
) -> str:
    canonical = [
        (name, os.environ.get(name, "").strip())
        for name in canonical_names
        if os.environ.get(name, "").strip()
    ]
    legacy = [
        (name, os.environ.get(name, "").strip())
        for name in legacy_names
        if os.environ.get(name, "").strip()
    ]
    canonical_values = {value for _name, value in canonical}
    legacy_values = {value for _name, value in legacy}
    if len(canonical_values) > 1:
        raise BlackboardError(
            "conflicting canonical environment values: "
            + ", ".join(f"{name}={value!r}" for name, value in canonical)
        )
    if len(legacy_values) > 1:
        raise BlackboardError(
            "conflicting legacy environment values: "
            + ", ".join(f"{name}={value!r}" for name, value in legacy)
        )
    if canonical_values and legacy_values and canonical_values != legacy_values:
        raise BlackboardError(
            "canonical and legacy environment variables disagree; unset one set "
            "or make their values identical"
        )
    if canonical_values:
        return next(iter(canonical_values))
    if legacy_values:
        return next(iter(legacy_values))
    return ""


def _actor() -> str:
    actor = _configured_actor()
    if actor:
        return actor
    intent_id = _intent_id()
    return f"worker:{intent_id}" if intent_id else "worker"


def _configured_actor() -> str:
    return (
        _CLI_ACTOR
        or _compatible_env(
            ("MULTI_AGENT_COLLABORATION_WORKER_ID",), ("INFINITEX_WORKER_ID",)
        )
        or os.environ.get("CODEX_AGENT_ID", "").strip()
    )


def _stable_actor() -> str:
    actor = _configured_actor()
    if not actor:
        raise BlackboardError(
            "ownership commands require a stable worker id; set "
            "MULTI_AGENT_COLLABORATION_WORKER_ID "
            "(legacy: INFINITEX_WORKER_ID) or pass --actor before the subcommand"
        )
    return actor


def _intent_id() -> str:
    return _CLI_INTENT_ID or _compatible_env(
        ("MULTI_AGENT_COLLABORATION_INTENT_ID",), ("INFINITEX_INTENT_ID",)
    )


def _resolve_marker(marker: Path) -> Path:
    """Resolve a marker as either a SQLite DB or a UTF-8 text path file."""
    try:
        head = marker.read_bytes()[: len(SQLITE_MAGIC)]
    except OSError as exc:
        raise BlackboardError(f"cannot read blackboard marker {marker}: {exc}") from exc
    if head == SQLITE_MAGIC:
        return marker.resolve()

    try:
        raw = marker.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise BlackboardError(f"invalid blackboard marker {marker}: {exc}") from exc
    if not raw:
        raise BlackboardError(f"blackboard marker is empty: {marker}")
    raw = os.path.expandvars(os.path.expanduser(raw.splitlines()[0].strip()))
    target = Path(raw)
    if not target.is_absolute():
        target = marker.parent / target
    return target.resolve()


def _db_path() -> Path:
    raw = _CLI_DB or _compatible_env(
        ("MULTI_AGENT_COLLABORATION_BLACKBOARD_DB",), ("INFINITEX_BLACKBOARD_DB",)
    )
    if raw:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if path.name in MARKER_NAMES and path.is_file():
            path = _resolve_marker(path)
    else:
        resolved_markers: list[tuple[str, Path]] = []
        for marker_name in MARKER_NAMES:
            marker = Path.cwd() / marker_name
            if marker.is_file():
                resolved_markers.append((marker_name, _resolve_marker(marker)))
        marker_targets = {str(target) for _name, target in resolved_markers}
        if len(marker_targets) > 1:
            raise BlackboardError(
                "blackboard markers disagree: "
                + ", ".join(f"{name}->{target}" for name, target in resolved_markers)
            )
        path = resolved_markers[0][1] if resolved_markers else None
        if path is None:
            direct_candidates: list[Path] = []
            for db_name in DIRECT_DB_NAMES:
                direct = Path.cwd() / db_name
                if direct.is_file():
                    direct_candidates.append(direct.resolve())
            if len(direct_candidates) > 1:
                raise BlackboardError(
                    "multiple fallback blackboard DB files exist; set "
                    "MULTI_AGENT_COLLABORATION_BLACKBOARD_DB explicitly"
                )
            path = direct_candidates[0] if direct_candidates else None
        if path is None:
            raise BlackboardError(
                "no blackboard DB: set MULTI_AGENT_COLLABORATION_BLACKBOARD_DB "
                "(legacy: INFINITEX_BLACKBOARD_DB), pass --db, or provide a supported "
                "marker/DB file in cwd"
            )

    if not path.is_file():
        raise BlackboardError(f"blackboard DB does not exist or is not a file: {path}")
    return path


@contextmanager
def _open_conn() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    try:
        conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    except sqlite3.Error as exc:
        raise BlackboardError(f"cannot open blackboard DB {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_tx(conn: sqlite3.Connection) -> Iterator[None]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in _columns(conn, table)


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    keyed = sorted(
        ((int(row[5] or 0), str(row[1])) for row in rows if int(row[5] or 0) > 0),
        key=lambda item: item[0],
    )
    return [name for _position, name in keyed]


def _is_unique_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if _primary_key_columns(conn, table) == [column]:
        return True
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        # index_list columns: seq, name, unique, origin, partial
        if not bool(index[2]):
            continue
        names = [str(row[2]) for row in conn.execute(f"PRAGMA index_info({index[1]})")]
        if names == [column]:
            return True
    return False


def _require_table(conn: sqlite3.Connection, table: str) -> None:
    if not _table_exists(conn, table):
        raise BlackboardError(
            f"board schema is missing required table '{table}'; check --db or run the coordinator migration"
        )


def _safe_payload(raw: Any) -> tuple[dict[str, Any], bool]:
    if isinstance(raw, dict):
        return raw, True
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _display_text(value: Any, max_chars: int = 4000) -> str:
    """Render untrusted board text as one inert line for agent-facing output."""
    text = _ANSI_ESCAPE_RE.sub("", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _fact_identity(text: str) -> str:
    text = re.sub(r"^\[[a-z0-9 _.-]{1,40}\]\s*", "", text, flags=re.IGNORECASE)
    return _normalize_text(text)


def _challenge_id(conn: sqlite3.Connection) -> str:
    explicit = _CLI_CHALLENGE_ID or _compatible_env(
        ("MULTI_AGENT_COLLABORATION_RUN_ID",),
        ("INFINITEX_CHALLENGE_ID",),
    )

    # The coordinator's SQLiteSharedGraph invariant is one DB per challenge.  Lock
    # IDs are intentionally challenge-local, so a mixed DB would make otherwise
    # scoped reads look safe while activity/resource mutual exclusion was not.
    known_ids: set[str] = set()
    for table in (
        "events",
        "intents",
        "operator_directives",
        "routes",
        "branches",
        "fact_states",
        "fact_reviews",
        "fact_pins",
        "activity_locks",
        "resource_locks",
        "lane_locks",
        "pocs",
        "hitl_requests",
    ):
        if _has_column(conn, table, "challenge_id"):
            for row in conn.execute(
                f"SELECT DISTINCT challenge_id FROM {table} "
                "WHERE challenge_id IS NOT NULL AND challenge_id != '' LIMIT 3"
            ):
                known_ids.add(str(row[0]))
                if len(known_ids) > 1:
                    break
        if len(known_ids) > 1:
            break
    if len(known_ids) > 1:
        raise BlackboardError(
            "the DB contains multiple run/challenge IDs; this project requires one board DB per run"
        )
    if explicit:
        if known_ids and explicit not in known_ids:
            only = next(iter(known_ids))
            raise BlackboardError(
                f"challenge scope mismatch: requested {explicit!r}, DB contains {only!r}"
            )
        return explicit

    intent_id = _intent_id()
    if intent_id and _table_exists(conn, "intents"):
        cols = _columns(conn, "intents")
        if {"intent_id", "challenge_id"}.issubset(cols):
            rows = conn.execute(
                "SELECT DISTINCT challenge_id FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchall()
            ids = {str(row[0]) for row in rows if row[0] not in (None, "")}
            if len(ids) == 1:
                return ids.pop()

    if _table_exists(conn, "board_meta"):
        cols = _columns(conn, "board_meta")
        if {"key", "value"}.issubset(cols):
            row = conn.execute(
                "SELECT value FROM board_meta WHERE key='default_challenge_id'"
            ).fetchone()
            if row and row[0]:
                meta_id = str(row[0])
                if known_ids and meta_id not in known_ids:
                    only = next(iter(known_ids))
                    raise BlackboardError(
                        f"board_meta challenge {meta_id!r} disagrees with DB content {only!r}"
                    )
                return meta_id

    if len(known_ids) == 1:
        return known_ids.pop()
    return ""


def _write_challenge_id(conn: sqlite3.Connection) -> str:
    challenge_id = _challenge_id(conn)
    if not challenge_id:
        raise BlackboardError(
            "cannot mutate an unscoped/empty board; set "
            "MULTI_AGENT_COLLABORATION_RUN_ID "
            "(legacy: INFINITEX_CHALLENGE_ID) or pass --run-id"
        )
    return challenge_id


def _scoped_clause(
    conn: sqlite3.Connection, table: str, challenge_id: str, *, prefix: str = "WHERE"
) -> tuple[str, list[Any]]:
    if _has_column(conn, table, "challenge_id"):
        return f" {prefix} COALESCE(challenge_id, '')=?", [challenge_id]
    return "", []


def _event_select(
    conn: sqlite3.Connection,
    select: str,
    kinds: Sequence[str],
    challenge_id: str,
    *,
    order: str = "seq",
    after_seq: int = 0,
    limit: Optional[int] = None,
) -> list[sqlite3.Row]:
    _require_table(conn, "events")
    cols = _columns(conn, "events")
    if not {"kind", "payload"}.issubset(cols):
        raise BlackboardError("events table is missing kind/payload columns")
    placeholders = ",".join("?" for _ in kinds)
    where = f"kind IN ({placeholders})"
    params: list[Any] = list(kinds)
    if "challenge_id" in cols:
        where += " AND COALESCE(challenge_id, '')=?"
        params.append(challenge_id)
    seq_col = "seq" if "seq" in cols else "rowid"
    if after_seq > 0:
        where += f" AND {seq_col}>?"
        params.append(int(after_seq))
    sql = f"SELECT {select} FROM events WHERE {where} ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def _insert_event(
    conn: sqlite3.Connection,
    *,
    challenge_id: str,
    actor: str,
    kind: str,
    payload: dict[str, Any],
    verified: bool = False,
    confidence: float = 1.0,
    dedupe_key: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> int:
    _require_table(conn, "events")
    available = _columns(conn, "events")
    required = {"ts", "kind", "payload"}
    if not required.issubset(available):
        missing = ", ".join(sorted(required - available))
        raise BlackboardError(f"events table is missing required columns: {missing}")

    values: dict[str, Any] = {
        "ts": time.time(),
        "challenge_id": challenge_id,
        "actor": actor,
        "kind": kind,
        "payload": json.dumps(payload, ensure_ascii=False),
        "artifact_id": artifact_id,
        "verified": int(verified),
        "confidence": float(confidence),
        "dedupe_key": dedupe_key,
    }
    names = [name for name in values if name in available]
    placeholders = ",".join("?" for _ in names)
    cur = conn.execute(
        f"INSERT INTO events ({','.join(names)}) VALUES ({placeholders})",
        [values[name] for name in names],
    )
    return int(cur.lastrowid or 0)


def _is_duplicate_error(exc: sqlite3.IntegrityError) -> bool:
    msg = str(exc).lower()
    return "unique" in msg or "dedupe" in msg


def _fact_lifecycle(conn: sqlite3.Connection, challenge_id: str) -> dict[int, dict[str, Any]]:
    """Fold fact_states/fact_reviews into the effective worker-visible state."""
    lifecycle: dict[int, dict[str, Any]] = {}
    if _table_exists(conn, "fact_states"):
        cols = _columns(conn, "fact_states")
        if {"fact_seq", "state"}.issubset(cols):
            select = ["fact_seq", "state"]
            for optional in ("verified_effective", "confidence_effective", "retired_seq"):
                select.append(optional if optional in cols else f"NULL AS {optional}")
            where, params = _scoped_clause(conn, "fact_states", challenge_id)
            for row in conn.execute(
                f"SELECT {','.join(select)} FROM fact_states{where}", params
            ).fetchall():
                state = str(row["state"] or "unresolved")
                lifecycle[int(row["fact_seq"])] = {
                    "state": state,
                    "verified_effective": row["verified_effective"],
                    "confidence_effective": row["confidence_effective"],
                    "retired": row["retired_seq"] is not None
                    or state in {"rejected", "merged", "superseded"},
                }

    # fact_reviews is kept for compatibility with boards predating fact_states and
    # remains useful as a fallback if a state row has not been materialized yet.
    if _table_exists(conn, "fact_reviews"):
        cols = _columns(conn, "fact_reviews")
        if {"fact_seq", "status"}.issubset(cols):
            where, params = _scoped_clause(conn, "fact_reviews", challenge_id)
            for row in conn.execute(
                f"SELECT fact_seq,status FROM fact_reviews{where}", params
            ).fetchall():
                seq = int(row["fact_seq"])
                status = str(row["status"] or "")
                current = lifecycle.setdefault(seq, {})
                if status in {"challenged", "revalidated", "rejected", "merged", "superseded"}:
                    current.setdefault("state", status)
                if status in {"rejected", "merged", "superseded"}:
                    current["retired"] = True
                if status == "challenged":
                    current["state"] = "challenged"
    return lifecycle


def read_facts(
    verified_only: bool = False, limit: int = 200, since_seq: int = 0
) -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        scan_limit = min(5000, max(limit, limit * 5))
        cols = _columns(conn, "events")
        verified_expr = "verified" if "verified" in cols else "0"
        confidence_expr = "confidence" if "confidence" in cols else "NULL"
        actor_expr = "actor" if "actor" in cols else "''"
        seq_expr = "seq" if "seq" in cols else "rowid"
        rows = _event_select(
            conn,
            f"{seq_expr} AS seq,payload,{verified_expr} AS verified,"
            f"{confidence_expr} AS confidence,{actor_expr} AS actor",
            ("fact_added",),
            cid,
            order=f"{seq_expr} DESC",
            after_seq=since_seq,
            limit=scan_limit,
        )
        rows = list(reversed(rows))
        lifecycle = _fact_lifecycle(conn, cid)

    output: list[tuple[int, str, str, bool, float]] = []
    invalid = 0
    for row in rows:
        seq = int(row["seq"])
        state = lifecycle.get(seq, {})
        if state.get("retired"):
            continue
        verified = bool(row["verified"])
        confidence = _as_float(row["confidence"], 1.0 if verified else 0.4)
        effective_verified = state.get("verified_effective")
        effective_confidence = state.get("confidence_effective")
        if effective_verified is not None:
            verified = bool(effective_verified)
        if effective_confidence is not None:
            confidence = _as_float(effective_confidence, confidence)
        if state.get("state") == "challenged":
            verified = False
            confidence = min(confidence, 0.4)
        if verified_only and not verified:
            continue
        payload, valid = _safe_payload(row["payload"])
        if not valid:
            invalid += 1
            continue
        fact = _display_text(payload.get("fact", ""))
        if not fact:
            continue
        source = _display_text(payload.get("source") or row["actor"] or "unknown", 160)
        output.append((seq, fact, source, verified, confidence))

    if len(output) > limit:
        output = output[-limit:]

    if not output:
        print("(no facts on the board yet)")
    else:
        for seq, fact, source, verified, confidence in output:
            tag = "VERIFIED" if verified else f"candidate({confidence:.1f})"
            print(f"[#{seq}] [{tag}] ({source}) {fact}")
    if invalid:
        print(f"[WARN] ignored {invalid} malformed fact event(s)", file=sys.stderr)


def read_flags() -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        rows = _event_select(conn, "payload,kind", ("flag_found", "flag_invalidated"), cid)
    found: list[str] = []
    rejected: set[str] = set()
    for row in rows:
        payload, valid = _safe_payload(row["payload"])
        flag = str(payload.get("flag", "")).strip() if valid else ""
        if not flag:
            continue
        if row["kind"] == "flag_invalidated":
            rejected.add(flag)
            if flag in found:
                found.remove(flag)
        elif row["kind"] == "flag_found" and flag not in rejected and flag not in found:
            found.append(flag)
    if not found:
        print("(no flags recovered yet — you may be the first)")
        return
    print("# Flags already recovered by the team — do NOT re-submit these:")
    for flag in found:
        print(f"- {_display_text(flag, 1000)}")


def read_deadends(limit: int = 200, since_seq: int = 0) -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        cols = _columns(conn, "events")
        seq_expr = "seq" if "seq" in cols else "rowid"
        rows = _event_select(
            conn,
            f"{seq_expr} AS seq,payload",
            ("dead_end",),
            cid,
            order=f"{seq_expr} DESC",
            after_seq=since_seq,
            limit=limit,
        )
        rows = list(reversed(rows))
    if not rows:
        print("(no dead-ends recorded — nothing ruled out yet)")
        return
    print("# Dead-ends — directions already ruled out, DO NOT retry these:")
    for row in rows:
        payload, valid = _safe_payload(row["payload"])
        reason = _display_text(payload.get("reason", "")) if valid else ""
        print(f"- [#{row['seq']}] {reason or '[malformed dead-end event]'}")


def _event_payload_by_seq(conn: sqlite3.Connection, seq: int) -> dict[str, Any]:
    if not _table_exists(conn, "events"):
        return {}
    seq_col = "seq" if _has_column(conn, "events", "seq") else "rowid"
    row = conn.execute(f"SELECT payload FROM events WHERE {seq_col}=?", (int(seq),)).fetchone()
    if not row:
        return {}
    payload, _ = _safe_payload(row[0])
    return payload


def _read_routes(conn: sqlite3.Connection, cid: str) -> None:
    if not _table_exists(conn, "routes"):
        print("(this board has no route review table yet)")
        return
    cols = _columns(conn, "routes")
    if "route_hash" not in cols:
        print("(routes table is incompatible: missing route_hash)")
        return
    optional = {
        "label": "''",
        "status": "'open'",
        "reason": "''",
        "until_policy": "''",
        "suppressed_seq": "0",
        "reopened_seq": "0",
    }
    expr = ["route_hash"] + [
        name if name in cols else f"{fallback} AS {name}"
        for name, fallback in optional.items()
    ]
    where, params = _scoped_clause(conn, "routes", cid)
    rows = conn.execute(
        f"SELECT {','.join(expr)} FROM routes{where} "
        "ORDER BY MAX(COALESCE(suppressed_seq,0),COALESCE(reopened_seq,0)), route_hash",
        params,
    ).fetchall()
    if not rows:
        print("(no reviewed routes)")
        return
    print("# Reviewed routes")
    for route_hash, label, status, reason, until_policy, _suppressed, _reopened in rows:
        tag = "SUPPRESSED" if status == "suppressed" else "OPEN"
        safe_hash = _display_text(route_hash, 200)
        safe_label = _display_text(label or route_hash, 300)
        safe_reason = _display_text(reason, 1000)
        extra = (
            f" until={_display_text(until_policy, 300)}"
            if status == "suppressed" and until_policy
            else ""
        )
        print(f"[{tag}] {safe_hash} ({safe_label}){extra}: {safe_reason}")


def read_routes() -> None:
    with _open_conn() as conn:
        _read_routes(conn, _challenge_id(conn))


def _read_branches(conn: sqlite3.Connection, cid: str) -> None:
    if not _table_exists(conn, "branches"):
        print("(this board has no branch review table yet)")
        return
    cols = _columns(conn, "branches")
    if "branch_id" not in cols:
        print("(branches table is incompatible: missing branch_id)")
        return
    optional = {
        "parent_id": "''",
        "title": "''",
        "assumption": "''",
        "prove_or_disprove": "''",
        "status": "'open'",
        "created_seq": "0",
    }
    expr = ["branch_id"] + [
        name if name in cols else f"{fallback} AS {name}"
        for name, fallback in optional.items()
    ]
    where, params = _scoped_clause(conn, "branches", cid)
    rows = conn.execute(
        f"SELECT {','.join(expr)} FROM branches{where} ORDER BY created_seq, branch_id", params
    ).fetchall()
    if not rows:
        print("(no branch hypotheses)")
        return
    print("# Review branches — prove/disprove separately")
    for branch_id, parent_id, title, assumption, pod, status, _created in rows:
        parent = f" parent={_display_text(parent_id, 160)}" if parent_id else ""
        print(
            f"- [{_display_text(status or 'open', 40)}] "
            f"{_display_text(branch_id, 160)}{parent}: "
            f"{_display_text(title or assumption, 1000)}"
        )
        if assumption:
            print(f"  assumption: {_display_text(assumption, 1000)}")
        if pod:
            print(f"  prove/disprove: {_display_text(pod, 1000)}")


def read_branches() -> None:
    with _open_conn() as conn:
        _read_branches(conn, _challenge_id(conn))


def read_review() -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        print("# Review-Arbiter state")
        cols = _columns(conn, "events")
        seq_expr = "seq" if "seq" in cols else "rowid"
        actor_expr = "actor" if "actor" in cols else "''"
        rows = _event_select(
            conn,
            f"{seq_expr} AS seq,{actor_expr} AS actor,payload",
            ("review_finding",),
            cid,
            order=f"{seq_expr} DESC LIMIT 12",
        )
        if rows:
            print("\n## Findings")
            for row in reversed(rows):
                payload, valid = _safe_payload(row["payload"])
                if not valid:
                    print(
                        f"- #{row['seq']} [warning/malformed] "
                        f"{_display_text(row['actor'], 160)}: invalid payload"
                    )
                    continue
                severity = payload.get("severity", "info")
                kind = payload.get("kind", "finding")
                route = f" route={payload.get('route_hash')}" if payload.get("route_hash") else ""
                print(
                    f"- #{row['seq']} [{_display_text(severity, 40)}/"
                    f"{_display_text(kind, 80)}] {_display_text(row['actor'], 160)}:"
                    f"{_display_text(route, 220)} {_display_text(payload.get('summary', ''), 1200)}"
                )

        challenged: list[sqlite3.Row] = []
        if _table_exists(conn, "fact_reviews"):
            fr_cols = _columns(conn, "fact_reviews")
            if {"fact_seq", "status"}.issubset(fr_cols):
                reason = "reason" if "reason" in fr_cols else "'' AS reason"
                verification = (
                    "verification_intent_id"
                    if "verification_intent_id" in fr_cols
                    else "'' AS verification_intent_id"
                )
                order = "challenged_seq" if "challenged_seq" in fr_cols else "fact_seq"
                where = "status='challenged'"
                params: list[Any] = []
                if "challenge_id" in fr_cols:
                    where += " AND COALESCE(challenge_id, '')=?"
                    params.append(cid)
                challenged = conn.execute(
                    f"SELECT fact_seq,status,{reason},{verification} FROM fact_reviews "
                    f"WHERE {where} ORDER BY {order}",
                    params,
                ).fetchall()
        if challenged:
            print("\n## Challenged facts — do NOT rely on these until verified")
            for row in challenged:
                fact = _event_payload_by_seq(conn, int(row["fact_seq"])).get("fact", "")
                print(f"- fact #{row['fact_seq']}: {_display_text(fact, 1200)}")
                print(f"  reason: {_display_text(row['reason'], 1000)}")
                if row["verification_intent_id"]:
                    print(
                        f"  verify intent: {_display_text(row['verification_intent_id'], 160)}"
                    )

        directive_rows = _event_select(
            conn,
            f"{seq_expr} AS seq,{actor_expr} AS actor,payload",
            ("coordinator_directive",),
            cid,
            order=f"{seq_expr} DESC LIMIT 8",
        )
        if directive_rows:
            print("\n## Coordinator directives")
            for row in reversed(directive_rows):
                payload, valid = _safe_payload(row["payload"])
                if valid:
                    print(
                        f"- #{row['seq']} {_display_text(row['actor'], 160)} "
                        f"{_display_text(payload.get('action', 'note'), 80)}: "
                        f"{_display_text(payload.get('directive', ''), 1200)}"
                    )

        print("\n## Routes")
        _read_routes(conn, cid)
        print("\n## Branches")
        _read_branches(conn, cid)


def list_intents() -> None:
    with _open_conn() as conn:
        _require_table(conn, "intents")
        cid = _challenge_id(conn)
        cols = _columns(conn, "intents")
        if not {"intent_id", "goal", "status"}.issubset(cols):
            raise BlackboardError("intents table is missing intent_id/goal/status columns")
        select_cols = ["intent_id", "goal"]
        for optional in ("worker_class", "route_hash", "branch_id"):
            select_cols.append(optional if optional in cols else "''")
        where = "status='open'"
        params: list[Any] = []
        if "dispatch_state" in cols:
            where += " AND COALESCE(dispatch_state,'active')='active'"
        if "challenge_id" in cols:
            where += " AND COALESCE(challenge_id, '')=?"
            params.append(cid)
        order = "created_seq" if "created_seq" in cols else "intent_id"
        rows = conn.execute(
            f"SELECT {','.join(select_cols)} FROM intents WHERE {where} ORDER BY {order}", params
        ).fetchall()
    if not rows:
        print("(no open intents)")
        return
    print("# Open intents you can claim:")
    for intent_id, goal, worker_class, route_hash, branch_id in rows:
        meta = []
        if worker_class:
            meta.append(f"class={_display_text(worker_class, 80)}")
        if route_hash:
            meta.append(f"route={_display_text(route_hash, 160)}")
        if branch_id:
            meta.append(f"branch={_display_text(branch_id, 160)}")
        suffix = f" [{' '.join(meta)}]" if meta else ""
        print(f"- {_display_text(intent_id, 160)}: {_display_text(goal, 1000)}{suffix}")


def _intent_product_link_allowed(
    conn: sqlite3.Connection, challenge_id: str, actor: str, intent_id: str
) -> tuple[bool, str]:
    if not _table_exists(conn, "intents"):
        return False, "intents table is missing"
    cols = _columns(conn, "intents")
    required = {"intent_id", "status", "worker", "lease_until"}
    if not required.issubset(cols):
        return False, "intents table lacks ownership columns"
    select = ["status", "worker", "lease_until"]
    if "dispatch_state" in cols:
        select.append("dispatch_state")
    else:
        select.append("'active' AS dispatch_state")
    where = "intent_id=?"
    params: list[Any] = [intent_id]
    if "challenge_id" in cols:
        where += " AND COALESCE(challenge_id,'')=?"
        params.append(challenge_id)
    row = conn.execute(
        f"SELECT {','.join(select)} FROM intents WHERE {where}", params
    ).fetchone()
    if not row:
        return False, "intent does not exist in this challenge"
    if row["status"] != "claimed" or row["dispatch_state"] != "active":
        return False, "intent is not claimed+active"
    if row["worker"] != actor:
        return False, f"intent is owned by {row['worker'] or '<none>'}"
    lease_until = row["lease_until"]
    if lease_until is None or _as_float(lease_until, 0.0) <= time.time():
        return False, "intent lease has expired"
    return True, ""


def write_fact(text: str, verified: bool) -> None:
    text = text.strip()
    if not text:
        raise BlackboardError("fact text must not be empty")
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        intent_id = _intent_id()
        payload: dict[str, Any] = {
            "source": actor,
            "fact": text,
            "source_solver": actor,
            "witness": None,
            "verifier": actor if verified else "",
        }
        # Keep this byte-for-byte compatible with SQLiteSharedGraph.add_evidence so
        # the direct skill write and the worker's marker echo collide on one fact.
        key = f"fact::{actor}::{_fact_identity(text)}"
        upgraded = False
        duplicate = False
        intent_warning = ""
        try:
            with _write_tx(conn):
                link_intent = False
                if intent_id:
                    link_intent, intent_warning = _intent_product_link_allowed(
                        conn, cid, actor, intent_id
                    )
                    if link_intent:
                        payload["intent_id"] = intent_id
                    else:
                        payload["reported_intent_id"] = intent_id
                        payload["late_report"] = True
                event_cols = _columns(conn, "events")
                existing = None
                if "dedupe_key" in event_cols:
                    seq_col = "seq" if "seq" in event_cols else "rowid"
                    verified_expr = "verified" if "verified" in event_cols else "0"
                    existing = conn.execute(
                        f"SELECT {seq_col} AS seq,payload,{verified_expr} AS verified "
                        "FROM events WHERE dedupe_key=? AND kind='fact_added'",
                        (key,),
                    ).fetchone()

                if existing:
                    fact_seq = int(existing["seq"])
                    if verified and not bool(existing["verified"]):
                        assignments: list[str] = []
                        values: list[Any] = []
                        if "verified" in event_cols:
                            assignments.append("verified=1")
                        if "confidence" in event_cols:
                            assignments.append("confidence=?")
                            values.append(1.0)
                        if assignments:
                            values.append(fact_seq)
                            seq_col = "seq" if "seq" in event_cols else "rowid"
                            conn.execute(
                                f"UPDATE events SET {','.join(assignments)} WHERE {seq_col}=?",
                                values,
                            )
                        upgraded = True
                    else:
                        duplicate = True
                else:
                    fact_seq = _insert_event(
                        conn,
                        challenge_id=cid,
                        actor=actor,
                        kind="fact_added",
                        payload=payload,
                        verified=verified,
                        confidence=1.0 if verified else 0.4,
                        dedupe_key=key,
                    )

                if link_intent and fact_seq > 0 and _table_exists(conn, "intent_products"):
                    ip_cols = _columns(conn, "intent_products")
                    if {"intent_id", "fact_seq"}.issubset(ip_cols):
                        conn.execute(
                            "INSERT OR IGNORE INTO intent_products (intent_id,fact_seq) VALUES (?,?)",
                            (intent_id, fact_seq),
                        )
        except sqlite3.IntegrityError as exc:
            if _is_duplicate_error(exc):
                print("OK (duplicate fact, already on board)")
                return
            raise
    if intent_warning:
        print(
            f"WARN: fact was not linked to intent {intent_id}: {intent_warning}",
            file=sys.stderr,
        )
    if upgraded:
        print("OK upgraded existing fact to verified")
    elif duplicate:
        print("OK (duplicate fact, already on board)")
    else:
        print(f"OK wrote {'verified' if verified else 'candidate'} fact")


def mark_deadend(reason: str) -> None:
    reason = reason.strip()
    if not reason:
        raise BlackboardError("dead-end reason must not be empty")
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        key = f"deadend::{reason}"
        near_duplicate = False
        try:
            with _write_tx(conn):
                near_duplicate = _has_near_duplicate_deadend(conn, cid, reason)
                if not near_duplicate:
                    _insert_event(
                        conn,
                        challenge_id=cid,
                        actor=actor,
                        kind="dead_end",
                        payload={"reason": reason, "source": actor},
                        verified=False,
                        confidence=1.0,
                        dedupe_key=key,
                    )
        except sqlite3.IntegrityError as exc:
            if _is_duplicate_error(exc):
                print("OK (dead-end already recorded)")
                return
            raise
    print(
        "OK (near-duplicate dead-end already recorded)"
        if near_duplicate
        else "OK marked dead-end"
    )


def _normalize_deadend(text: str) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"\bthree\b", "3", value)
    value = re.sub(r"\btwo\b", "2", value)
    value = re.sub(r"\bone\b", "1", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _has_near_duplicate_deadend(
    conn: sqlite3.Connection, challenge_id: str, reason: str, threshold: float = 0.92
) -> bool:
    target = _normalize_deadend(reason)
    if not target:
        return False
    target_numbers = set(re.findall(r"\b\d+\b", target))
    cols = _columns(conn, "events")
    seq_col = "seq" if "seq" in cols else "rowid"
    rows = _event_select(
        conn,
        "payload",
        ("dead_end",),
        challenge_id,
        order=f"{seq_col} DESC LIMIT 200",
    )
    for row in rows:
        payload, valid = _safe_payload(row["payload"])
        old = _normalize_deadend(str(payload.get("reason", ""))) if valid else ""
        if not old:
            continue
        if target_numbers != set(re.findall(r"\b\d+\b", old)):
            continue
        if SequenceMatcher(None, target, old).ratio() >= threshold:
            return True
    return False


def _validate_intent_schema(conn: sqlite3.Connection) -> set[str]:
    _require_table(conn, "intents")
    cols = _columns(conn, "intents")
    required = {"intent_id", "status", "worker", "lease_until"}
    if not required.issubset(cols):
        raise BlackboardError(
            "intents table is missing columns required for leasing: "
            + ", ".join(sorted(required - cols))
        )
    if _primary_key_columns(conn, "intents") != ["intent_id"]:
        raise BlackboardError("intents table is incompatible: intent_id must be the primary key")
    return cols


def claim(intent_id: str, lease_seconds: float = 300.0) -> None:
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        cols = _validate_intent_schema(conn)
        now = time.time()
        with _write_tx(conn):
            where = "intent_id=?"
            params: list[Any] = [intent_id]
            if "challenge_id" in cols:
                where += " AND COALESCE(challenge_id, '')=?"
                params.append(cid)
            row = conn.execute(
                f"SELECT status,worker,lease_until FROM intents WHERE {where}", params
            ).fetchone()
            won = False
            if row:
                status, owner, lease_until = row
                expired = status == "claimed" and (
                    lease_until is None or _as_float(lease_until, now + 1) < now
                )
                active = "dispatch_state" not in cols
                if "dispatch_state" in cols:
                    ds = conn.execute(
                        f"SELECT dispatch_state FROM intents WHERE {where}", params
                    ).fetchone()
                    active = bool(ds and ds[0] in (None, "", "active"))
                if active and (status == "open" or expired):
                    update_params: list[Any] = [actor, now + lease_seconds] + params
                    cur = conn.execute(
                        f"UPDATE intents SET worker=?,status='claimed',lease_until=? WHERE {where}",
                        update_params,
                    )
                    won = cur.rowcount == 1
            if won:
                _insert_event(
                    conn,
                    challenge_id=cid,
                    actor=actor,
                    kind="intent_claimed",
                    payload={"intent_id": intent_id, "lease_seconds": lease_seconds},
                    confidence=1.0,
                )
    print("WON" if won else "LOST")


def renew_intent(intent_id: str, lease_seconds: float = 300.0) -> None:
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        cols = _validate_intent_schema(conn)
        where = "intent_id=? AND status='claimed' AND worker=?"
        params: list[Any] = [intent_id, actor]
        if "challenge_id" in cols:
            where += " AND COALESCE(challenge_id, '')=?"
            params.append(cid)
        if "dispatch_state" in cols:
            where += " AND dispatch_state='active'"
        now = time.time()
        where += " AND lease_until IS NOT NULL AND lease_until>?"
        params.append(now)
        with _write_tx(conn):
            cur = conn.execute(
                f"UPDATE intents SET lease_until=? WHERE {where}",
                [now + lease_seconds] + params,
            )
            won = cur.rowcount == 1
            if won:
                _insert_event(
                    conn,
                    challenge_id=cid,
                    actor=actor,
                    kind="intent_claimed",
                    payload={
                        "intent_id": intent_id,
                        "lease_seconds": lease_seconds,
                        "renewed": True,
                    },
                    confidence=1.0,
                )
    print("WON" if won else "LOST")


def release_intent(intent_id: str) -> None:
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        cols = _validate_intent_schema(conn)
        where = "intent_id=? AND status='claimed' AND worker=?"
        params: list[Any] = [intent_id, actor]
        if "challenge_id" in cols:
            where += " AND COALESCE(challenge_id, '')=?"
            params.append(cid)
        if "dispatch_state" in cols:
            where += " AND dispatch_state='active'"
        where += " AND lease_until IS NOT NULL AND lease_until>?"
        params.append(time.time())
        with _write_tx(conn):
            cur = conn.execute(
                f"UPDATE intents SET status='open',worker=NULL,lease_until=NULL WHERE {where}", params
            )
            won = cur.rowcount == 1
            if won:
                _insert_event(
                    conn,
                    challenge_id=cid,
                    actor=actor,
                    kind="intent_state_changed",
                    payload={
                        "intent_id": intent_id,
                        "status": "open",
                        "dispatch_state": "active",
                        "reason": "released_by_worker",
                    },
                    confidence=1.0,
                )
    print("OK" if won else "LOST")


def complete_intent(
    intent_id: str,
    result: str = "explored",
    detail: str = "",
    to_fact_seq: Optional[int] = None,
) -> None:
    """Conclude an intent with the same owner fence/state used by the coordinator."""
    clean_result = (result or "").strip()
    close_reason = (clean_result or "concluded")[:200]
    clean_detail = (detail or "").strip()
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        cols = _validate_intent_schema(conn)
        where = "intent_id=?"
        identity_params: list[Any] = [intent_id]
        if "challenge_id" in cols:
            where += " AND COALESCE(challenge_id,'')=?"
            identity_params.append(cid)
        with _write_tx(conn):
            current = conn.execute(
                f"SELECT worker,status FROM intents WHERE {where}", identity_params
            ).fetchone()
            owns_claim = bool(
                current
                and (
                    current["worker"] in (None, "", actor)
                    or actor == "coordinator"
                    or clean_result == "solved"
                )
            )
            payload: dict[str, Any] = {
                "intent_id": intent_id,
                "result": clean_result,
            }
            if current and not owns_claim:
                payload["late_report"] = True
                payload["current_owner"] = current["worker"]
            if clean_detail:
                payload["result_detail"] = clean_detail
            if to_fact_seq is not None:
                payload["to_fact_seq"] = int(to_fact_seq)
            seq = _insert_event(
                conn,
                challenge_id=cid,
                actor=actor,
                kind="intent_concluded",
                payload=payload,
                confidence=1.0,
            )

            if not current:
                outcome = "LOST"
            else:
                assignments = ["status='done'"]
                values: list[Any] = []
                if "dispatch_state" in cols:
                    assignments.append("dispatch_state='closed'")
                if "close_reason" in cols:
                    assignments.append("close_reason=?")
                    values.append(close_reason)
                if "result_seq" in cols:
                    assignments.append("result_seq=?")
                    values.append(seq if seq > 0 else None)
                if "result_detail" in cols:
                    assignments.append("result_detail=?")
                    values.append(clean_detail or None)
                if to_fact_seq is not None and "to_fact_seq" in cols:
                    assignments.append("to_fact_seq=?")
                    values.append(int(to_fact_seq))

                fenced_where = where
                update_params = values + identity_params
                if clean_result != "solved" and actor != "coordinator":
                    fenced_where += " AND (worker=? OR worker IS NULL)"
                    update_params.append(actor)
                cur = conn.execute(
                    f"UPDATE intents SET {','.join(assignments)} WHERE {fenced_where}",
                    update_params,
                )
                outcome = "OK" if cur.rowcount == 1 else "STALE"
                if cur.rowcount == 1 and _is_genuine_giveup(clean_result):
                    _mark_intent_pocs_spent(conn, cid, intent_id, seq)
    print(outcome)


def _is_genuine_giveup(result: str) -> bool:
    return (result or "").strip().lower().split(":", 1)[0].strip() == "dead_end"


def _mark_intent_pocs_spent(
    conn: sqlite3.Connection, challenge_id: str, intent_id: str, result_seq: int
) -> None:
    if not _table_exists(conn, "pocs"):
        return
    cols = _columns(conn, "pocs")
    required = {"challenge_id", "intent_id", "status"}
    if not required.issubset(cols):
        return
    assignments = ["status='spent'"]
    params: list[Any] = []
    if "result_seq" in cols:
        assignments.append("result_seq=?")
        params.append(result_seq if result_seq > 0 else None)
    params.extend((challenge_id, intent_id))
    conn.execute(
        f"UPDATE pocs SET {','.join(assignments)} WHERE challenge_id=? AND intent_id=? "
        "AND status IN ('available','wip','directional')",
        params,
    )


def _norm_activity_key(key: str) -> str:
    value = (key or "").strip().lower()
    value = re.sub(r"[\s/]+", ":", value)
    return re.sub(r":+", ":", value).strip(":")


def _ensure_activity_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS activity_locks ("
        "activity_key TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, "
        "worker TEXT NOT NULL, lease_until REAL NOT NULL, claimed_ts REAL NOT NULL"
        ")"
    )
    required = {"activity_key", "challenge_id", "worker", "lease_until", "claimed_ts"}
    missing = required - _columns(conn, "activity_locks")
    if missing:
        raise BlackboardError(
            "activity_locks table is incompatible; missing: " + ", ".join(sorted(missing))
        )
    if _primary_key_columns(conn, "activity_locks") != ["activity_key"]:
        raise BlackboardError(
            "activity_locks is incompatible: activity_key must be the sole primary key"
        )


def claim_activity(key: str, lease_seconds: float = 600.0) -> None:
    normalized = _norm_activity_key(key)
    if not normalized:
        raise BlackboardError("activity key must not be empty")
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        _ensure_activity_table(conn)
        now = time.time()
        won = False
        with _write_tx(conn):
            row = conn.execute(
                "SELECT worker,lease_until FROM activity_locks WHERE activity_key=?",
                (normalized,),
            ).fetchone()
            if row and _as_float(row["lease_until"], now + 1) < now:
                cur = conn.execute(
                    "UPDATE activity_locks SET challenge_id=?,worker=?,lease_until=?,claimed_ts=? "
                    "WHERE activity_key=?",
                    (cid, actor, now + lease_seconds, now, normalized),
                )
                won = cur.rowcount == 1
            elif not row:
                try:
                    conn.execute(
                        "INSERT INTO activity_locks "
                        "(activity_key,challenge_id,worker,lease_until,claimed_ts) VALUES (?,?,?,?,?)",
                        (normalized, cid, actor, now + lease_seconds, now),
                    )
                    won = True
                except sqlite3.IntegrityError:
                    # A concurrent claimant won between SELECT and INSERT.
                    won = False
    print("WON" if won else "LOST")


def release_activity(key: str) -> None:
    normalized = _norm_activity_key(key)
    if not normalized:
        raise BlackboardError("activity key must not be empty")
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        if not _table_exists(conn, "activity_locks"):
            print("OK")
            return
        with _write_tx(conn):
            cur = conn.execute(
                "DELETE FROM activity_locks WHERE challenge_id=? AND activity_key=? "
                "AND worker=? AND lease_until>?",
                (cid, normalized, actor, time.time()),
            )
            won = cur.rowcount >= 1
    print("OK" if won else "LOST")


def renew_activity(key: str, lease_seconds: float = 600.0) -> None:
    normalized = _norm_activity_key(key)
    if not normalized:
        raise BlackboardError("activity key must not be empty")
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        if not _table_exists(conn, "activity_locks"):
            print("LOST")
            return
        _ensure_activity_table(conn)
        now = time.time()
        with _write_tx(conn):
            cur = conn.execute(
                "UPDATE activity_locks SET lease_until=?,claimed_ts=? "
                "WHERE challenge_id=? AND activity_key=? AND worker=? AND lease_until>?",
                (now + lease_seconds, now, cid, normalized, actor, now),
            )
            won = cur.rowcount == 1
    print("WON" if won else "LOST")


def list_activities() -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        if not _table_exists(conn, "activity_locks"):
            rows: list[sqlite3.Row] = []
        else:
            rows = conn.execute(
                "SELECT activity_key,worker,lease_until FROM activity_locks "
                "WHERE challenge_id=? AND lease_until>? ORDER BY claimed_ts",
                (cid, time.time()),
            ).fetchall()
    if not rows:
        print("(no activities in progress)")
        return
    for row in rows:
        remaining = max(0, int(_as_float(row["lease_until"], 0) - time.time()))
        print(
            f"{_display_text(row['activity_key'], 500)}  "
            f"[{_display_text(row['worker'], 160)}, lease={remaining}s]"
        )


def _normalize_resource_key(key: str) -> str:
    value = re.sub(r"\s+", "", (key or "").strip().lower())
    return re.sub(r"[^a-z0-9_:@.*/-]+", "-", value).strip("-")[:180]


def _ensure_resource_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS resource_locks ("
        "lock_id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, resource_key TEXT NOT NULL, "
        "scope TEXT NOT NULL, risk_class TEXT, status TEXT NOT NULL DEFAULT 'requested', "
        "owner_worker TEXT, owner_intent TEXT, lease_until REAL, created_seq INTEGER, "
        "released_seq INTEGER, conflict_policy TEXT NOT NULL DEFAULT 'exclusive', "
        "cooldown_s REAL NOT NULL DEFAULT 0)"
    )
    required = {
        "lock_id",
        "challenge_id",
        "resource_key",
        "scope",
        "status",
        "owner_worker",
        "lease_until",
    }
    missing = required - _columns(conn, "resource_locks")
    if missing:
        raise BlackboardError(
            "resource_locks table is incompatible; missing: " + ", ".join(sorted(missing))
        )
    if _primary_key_columns(conn, "resource_locks") != ["lock_id"]:
        raise BlackboardError("resource_locks is incompatible: lock_id must be the primary key")


def _resource_lock_id(resource_key: str) -> str:
    # Must match SQLiteSharedGraph.request_resource_lock exactly; the coordinator
    # addresses resource rows by this ID rather than by resource_key.
    return f"rl-{resource_key}"


def claim_resource(
    resource_key: str,
    scope: str = "activity",
    risk_class: str = "",
    lease_seconds: float = 600.0,
) -> None:
    normalized = _normalize_resource_key(resource_key)
    if not normalized:
        raise BlackboardError("resource key must not be empty")
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        intent_id = _intent_id()
        _ensure_resource_table(conn)
        cols = _columns(conn, "resource_locks")
        now = time.time()
        won = False
        with _write_tx(conn):
            row = conn.execute(
                "SELECT * FROM resource_locks WHERE challenge_id=? AND resource_key=? "
                "ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END LIMIT 1",
                (cid, normalized),
            ).fetchone()
            expected_lock_id = _resource_lock_id(normalized)
            if row and str(row["lock_id"]) != expected_lock_id:
                raise BlackboardError(
                    "resource_locks contains a non-canonical lock_id for this key; "
                    "run the coordinator migration before claiming it"
                )
            lock_id = expected_lock_id
            if row:
                expired = row["lease_until"] is None or _as_float(row["lease_until"], now + 1) < now
                own = row["status"] == "active" and row["owner_worker"] == actor
                available = row["status"] != "active" or expired or own or row["owner_worker"] is None
                if available:
                    assignments = [
                        "status='active'",
                        "owner_worker=?",
                        "scope=?",
                        "lease_until=?",
                    ]
                    values: list[Any] = [actor, scope or "activity", now + lease_seconds]
                    if "risk_class" in cols:
                        assignments.append("risk_class=?")
                        values.append(risk_class or None)
                    if "owner_intent" in cols:
                        assignments.append("owner_intent=?")
                        values.append(intent_id or None)
                    values.append(row["lock_id"])
                    cur = conn.execute(
                        f"UPDATE resource_locks SET {','.join(assignments)} WHERE lock_id=?", values
                    )
                    won = cur.rowcount == 1
            else:
                names = [
                    "lock_id",
                    "challenge_id",
                    "resource_key",
                    "scope",
                    "status",
                    "owner_worker",
                    "lease_until",
                ]
                values = [
                    lock_id,
                    cid,
                    normalized,
                    scope or "activity",
                    "active",
                    actor,
                    now + lease_seconds,
                ]
                if "risk_class" in cols:
                    names.append("risk_class")
                    values.append(risk_class or None)
                if "owner_intent" in cols:
                    names.append("owner_intent")
                    values.append(intent_id or None)
                try:
                    conn.execute(
                        f"INSERT INTO resource_locks ({','.join(names)}) "
                        f"VALUES ({','.join('?' for _ in names)})",
                        values,
                    )
                    won = True
                except sqlite3.IntegrityError:
                    won = False
            if won:
                seq = _insert_event(
                    conn,
                    challenge_id=cid,
                    actor=actor,
                    kind="resource_locked",
                    payload={
                        "lock_id": lock_id,
                        "resource_key": normalized,
                        "scope": scope or "activity",
                        "risk_class": risk_class,
                        "owner_worker": actor,
                        "owner_intent": intent_id,
                    },
                    confidence=1.0,
                )
                if row is None and seq and "created_seq" in cols:
                    conn.execute(
                        "UPDATE resource_locks SET created_seq=? "
                        "WHERE challenge_id=? AND resource_key=?",
                        (seq, cid, normalized),
                    )
    print("WON" if won else "LOST")


def release_resource(resource_key: str) -> None:
    normalized = _normalize_resource_key(resource_key)
    if not normalized:
        raise BlackboardError("resource key must not be empty")
    with _open_conn() as conn:
        cid = _write_challenge_id(conn)
        actor = _stable_actor()
        if not _table_exists(conn, "resource_locks"):
            print("OK")
            return
        cols = _columns(conn, "resource_locks")
        assignments = ["status='released'", "owner_worker=NULL", "lease_until=NULL"]
        with _write_tx(conn):
            row = conn.execute(
                "SELECT lock_id,owner_worker,lease_until FROM resource_locks "
                "WHERE challenge_id=? AND resource_key=? AND status='active' LIMIT 1",
                (cid, normalized),
            ).fetchone()
            expected_lock_id = _resource_lock_id(normalized)
            if row and str(row["lock_id"]) != expected_lock_id:
                raise BlackboardError(
                    "resource_locks contains a non-canonical lock_id for this key; "
                    "run the coordinator migration before releasing it"
                )
            lock_id = expected_lock_id
            releasable = bool(
                row
                and (
                    not row["owner_worker"]
                    or row["owner_worker"] == actor
                    or row["lease_until"] is None
                    or _as_float(row["lease_until"], time.time() + 1) < time.time()
                )
            )
            if releasable:
                cur = conn.execute(
                    f"UPDATE resource_locks SET {','.join(assignments)} "
                    "WHERE challenge_id=? AND lock_id=? AND status='active'",
                    (cid, lock_id),
                )
                won = cur.rowcount >= 1
            else:
                won = False
            if won:
                seq = _insert_event(
                    conn,
                    challenge_id=cid,
                    actor=actor,
                    kind="resource_released",
                    payload={
                        "lock_id": lock_id,
                        "resource_key": normalized,
                        "released_by": actor,
                    },
                    confidence=1.0,
                )
                if seq and "released_seq" in cols:
                    conn.execute(
                        "UPDATE resource_locks SET released_seq=? "
                        "WHERE challenge_id=? AND resource_key=?",
                        (seq, cid, normalized),
                    )
    print("OK" if won else "LOST")


def read_resource_locks() -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        if not _table_exists(conn, "resource_locks"):
            rows: list[sqlite3.Row] = []
        else:
            cols = _columns(conn, "resource_locks")
            required = {
                "resource_key",
                "scope",
                "status",
                "owner_worker",
                "lease_until",
            }
            if not required.issubset(cols):
                raise BlackboardError("resource_locks table has an incompatible schema")
            risk = "risk_class" if "risk_class" in cols else "NULL AS risk_class"
            order = "COALESCE(created_seq,0),resource_key" if "created_seq" in cols else "resource_key"
            where = "status='active' AND owner_worker IS NOT NULL "
            params: list[Any] = [time.time()]
            if "challenge_id" in cols:
                where += "AND COALESCE(challenge_id,'')=? "
                params.insert(0, cid)
            where += "AND (lease_until IS NULL OR lease_until>?)"
            rows = conn.execute(
                f"SELECT resource_key,scope,{risk},owner_worker,lease_until "
                f"FROM resource_locks WHERE {where} ORDER BY {order}",
                params,
            ).fetchall()
    if not rows:
        print("(no resource locks held)")
        return
    print("# Active resource locks (do NOT conflict):")
    for row in rows:
        risk = (
            f" risk={_display_text(row['risk_class'], 80)}" if row["risk_class"] else ""
        )
        remaining = (
            ""
            if row["lease_until"] is None
            else f" lease={max(0, int(_as_float(row['lease_until'], 0) - time.time()))}s"
        )
        print(
            f"- {_display_text(row['resource_key'], 500)} "
            f"(scope={_display_text(row['scope'], 80)}{risk}{remaining}) "
            f"[{_display_text(row['owner_worker'], 160)}]"
        )


def read_directives() -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        if not _table_exists(conn, "operator_directives"):
            rows: list[sqlite3.Row] = []
        else:
            cols = _columns(conn, "operator_directives")
            required = {"directive_id", "action", "text", "status"}
            if not required.issubset(cols):
                raise BlackboardError("operator_directives table has an incompatible schema")
            priority = "priority" if "priority" in cols else "0 AS priority"
            order = "received_seq" if "received_seq" in cols else "directive_id"
            where = "status NOT IN ('superseded','expired','rejected')"
            params: list[Any] = []
            if "challenge_id" in cols:
                where += " AND COALESCE(challenge_id, '')=?"
                params.append(cid)
            rows = conn.execute(
                f"SELECT directive_id,action,text,status,{priority} "
                f"FROM operator_directives WHERE {where} ORDER BY priority DESC,{order}",
                params,
            ).fetchall()
    if not rows:
        print("(no active operator directives)")
        return
    print("# Operator directives (highest-priority guidance; not evidence):")
    for row in rows:
        print(
            f"- [{_display_text(row['action'], 80)}/{_display_text(row['status'], 80)}] "
            f"{_display_text(row['text'], 1200)} "
            f"(id={_display_text(row['directive_id'], 160)}, priority={row['priority']})"
        )


def directive_status(directive_id: str) -> None:
    with _open_conn() as conn:
        cid = _challenge_id(conn)
        if not _table_exists(conn, "operator_directives"):
            print("(unknown)")
            return
        cols = _columns(conn, "operator_directives")
        bound = "bound_worker" if "bound_worker" in cols else "'' AS bound_worker"
        where = "directive_id=?"
        params: list[Any] = [directive_id]
        if "challenge_id" in cols:
            where += " AND COALESCE(challenge_id, '')=?"
            params.append(cid)
        row = conn.execute(
            f"SELECT action,text,status,{bound} FROM operator_directives WHERE {where}", params
        ).fetchone()
    if not row:
        print("(unknown directive)")
        return
    suffix = (
        f" bound={_display_text(row['bound_worker'], 160)}" if row["bound_worker"] else ""
    )
    print(
        f"{_display_text(directive_id, 160)}: {_display_text(row['action'], 80)} "
        f"status={_display_text(row['status'], 80)}{suffix} :: "
        f"{_display_text(row['text'], 1200)}"
    )


def doctor(strict: bool = False) -> bool:
    path = _db_path()
    with _open_conn() as conn:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        quick_check = str(conn.execute("PRAGMA quick_check(1)").fetchone()[0])
        try:
            cid = _challenge_id(conn)
            challenge_line = cid or "<legacy-empty>"
            challenge_ok = bool(cid)
        except BlackboardError as exc:
            challenge_line = f"ERROR: {exc}"
            challenge_ok = False
        missing = [table for table in ("events", "intents") if table not in tables]
        schema_errors: list[str] = []
        if "events" in tables:
            event_cols = _columns(conn, "events")
            event_required = {
                "seq",
                "ts",
                "challenge_id",
                "actor",
                "kind",
                "payload",
                "artifact_id",
                "verified",
                "confidence",
                "dedupe_key",
            }
            if not event_required.issubset(event_cols):
                schema_errors.append(
                    "events missing " + ",".join(sorted(event_required - event_cols))
                )
            if _primary_key_columns(conn, "events") != ["seq"]:
                schema_errors.append("events.seq is not the primary key")
            if "dedupe_key" in event_cols and not _is_unique_column(
                conn, "events", "dedupe_key"
            ):
                schema_errors.append("events.dedupe_key is not UNIQUE")
        if "intents" in tables and _primary_key_columns(conn, "intents") != ["intent_id"]:
            schema_errors.append("intents.intent_id is not the primary key")
        for table, key in (
            ("activity_locks", "activity_key"),
            ("resource_locks", "lock_id"),
            ("routes", "route_hash"),
            ("branches", "branch_id"),
            ("operator_directives", "directive_id"),
        ):
            if table in tables and _primary_key_columns(conn, table) != [key]:
                schema_errors.append(f"{table}.{key} is not the sole primary key")
        if "resource_locks" in tables:
            rl_cols = _columns(conn, "resource_locks")
            if {"lock_id", "resource_key"}.issubset(rl_cols):
                bad = conn.execute(
                    "SELECT lock_id,resource_key FROM resource_locks "
                    "WHERE lock_id != 'rl-' || resource_key LIMIT 1"
                ).fetchone()
                if bad:
                    schema_errors.append(
                        f"resource_locks has non-canonical id {bad['lock_id']!r} "
                        f"for {bad['resource_key']!r}"
                    )
        if strict:
            strict_columns: dict[str, set[str]] = {
                "events": {
                    "seq", "ts", "challenge_id", "actor", "kind", "payload",
                    "artifact_id", "verified", "confidence", "dedupe_key",
                },
                "intents": {
                    "intent_id", "challenge_id", "goal", "worker_class", "route_hash",
                    "branch_id", "lane_key", "risk_class", "lane_deferrals",
                    "deferred_against_locked_seq", "priority", "status", "worker",
                    "lease_until", "created_seq", "result_seq", "result_detail",
                    "to_fact_seq", "summary", "dispatch_state", "close_reason",
                    "stop_reason", "superseded_by_intent_id",
                    "superseded_by_directive_id", "resource_key", "resource_lock_id",
                    "compact_id", "directive_id",
                },
                "activity_locks": {
                    "activity_key", "challenge_id", "worker", "lease_until", "claimed_ts",
                },
                "resource_locks": {
                    "lock_id", "challenge_id", "resource_key", "scope", "risk_class",
                    "status", "owner_worker", "owner_intent", "lease_until", "created_seq",
                    "released_seq", "conflict_policy", "cooldown_s",
                },
                "operator_directives": {
                    "directive_id", "challenge_id", "action", "text", "scope", "priority",
                    "standing", "status", "preempt_policy", "generated_fact_seq",
                    "generated_intent_id", "bound_worker", "conflicts_json", "received_seq",
                    "queued_seq", "bound_seq", "acted_seq", "superseded_seq",
                },
                "routes": {
                    "route_hash", "challenge_id", "label", "status", "suppressed_seq",
                    "reopened_seq", "reason", "until_policy",
                },
                "branches": {
                    "branch_id", "challenge_id", "parent_id", "title", "assumption",
                    "prove_or_disprove", "status", "created_seq", "resolved_seq",
                },
            }
            for table, expected in strict_columns.items():
                if table not in tables:
                    schema_errors.append(f"strict schema missing table {table}")
                    continue
                missing_columns = expected - _columns(conn, table)
                if missing_columns:
                    schema_errors.append(
                        f"{table} missing " + ",".join(sorted(missing_columns))
                    )

    actor = _actor()
    actor_ok = bool(_configured_actor())
    print(f"DB: {path}")
    print(f"SQLite: {sqlite3.sqlite_version} (journal={journal})")
    print(f"Integrity: {quick_check}")
    print(f"Run/Challenge: {challenge_line}")
    print(f"Actor: {actor}")
    print(f"Intent: {_intent_id() or '<unset>'}")
    print(f"Tables: {', '.join(tables) if tables else '<none>'}")
    print(f"Schema mode: {'strict-upstream' if strict else 'compatible'}")
    print(f"Mutation ready: {'yes' if actor_ok and challenge_ok else 'no'}")
    if not actor_ok:
        print(
            "WARN: no stable worker id; set MULTI_AGENT_COLLABORATION_WORKER_ID "
            "or pass --actor"
        )
    if not challenge_ok and not challenge_line.startswith("ERROR:"):
        print(
            "WARN: no non-empty run id; set MULTI_AGENT_COLLABORATION_RUN_ID "
            "or pass --run-id"
        )
    if str(journal).lower() != "wal":
        print("WARN: journal_mode is not WAL; concurrent writers may serialize more often")
    if missing:
        print(f"ERROR: missing core table(s): {', '.join(missing)}")
    for error in schema_errors:
        print(f"ERROR: {error}")
    if quick_check.lower() != "ok":
        print(f"ERROR: SQLite quick_check failed: {quick_check}")
    return (
        not missing
        and not schema_errors
        and quick_check.lower() == "ok"
        and challenge_ok
        and actor_ok
    )


def _positive_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    if value > 86400:
        raise argparse.ArgumentTypeError("must be <= 86400 seconds")
    return value


def _bounded_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0 or value > 5000:
        raise argparse.ArgumentTypeError("must be between 1 and 5000")
    return value


def _nonnegative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blackboard.py",
        description="Coordinate multi-agent work through a shared SQLite blackboard.",
    )
    parser.add_argument(
        "--db",
        help=(
            "DB path (overrides MULTI_AGENT_COLLABORATION_BLACKBOARD_DB and legacy "
            "INFINITEX_BLACKBOARD_DB)"
        ),
    )
    parser.add_argument(
        "--run-id",
        "--challenge-id",
        dest="challenge_id",
        metavar="RUN_ID",
        help="run scope (legacy alias: --challenge-id)",
    )
    parser.add_argument(
        "--actor",
        help=(
            "stable worker id (overrides MULTI_AGENT_COLLABORATION_WORKER_ID and "
            "legacy INFINITEX_WORKER_ID)"
        ),
    )
    parser.add_argument(
        "--intent-id",
        dest="context_intent_id",
        help=(
            "current intent id (overrides MULTI_AGENT_COLLABORATION_INTENT_ID and "
            "legacy INFINITEX_INTENT_ID)"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read-facts", help="read active facts")
    p.add_argument("--verified-only", action="store_true")
    p.add_argument("--limit", type=_bounded_limit, default=200)
    p.add_argument("--since-seq", type=_nonnegative_int, default=0)
    sub.add_parser("read-review", help="read review findings, challenged facts, routes and branches")
    sub.add_parser("read-routes", help="read suppressed/reopened routes")
    sub.add_parser("read-branches", help="read branch hypotheses")
    p = sub.add_parser("read-deadends", help="read ruled-out directions")
    p.add_argument("--limit", type=_bounded_limit, default=200)
    p.add_argument("--since-seq", type=_nonnegative_int, default=0)
    sub.add_parser("read-flags", help="read currently valid recovered flags")
    sub.add_parser("list-intents", help="list active open intents")

    p = sub.add_parser("write-fact", help="append a candidate or verified fact")
    p.add_argument("text")
    p.add_argument("--verified", action="store_true")
    p = sub.add_parser("mark-deadend", help="append a ruled-out direction")
    p.add_argument("reason")

    p = sub.add_parser("claim", help="atomically claim an open or abandoned intent")
    p.add_argument("target_intent_id")
    p.add_argument("--lease-seconds", type=_positive_seconds, default=300.0)
    p = sub.add_parser("renew-intent", help="renew an intent owned by this worker")
    p.add_argument("target_intent_id")
    p.add_argument("--lease-seconds", type=_positive_seconds, default=300.0)
    p = sub.add_parser("release-intent", help="return an owned intent to the open pool")
    p.add_argument("target_intent_id")
    p = sub.add_parser("complete-intent", help="owner-fenced terminal intent conclusion")
    p.add_argument("target_intent_id")
    p.add_argument("--result", default="explored")
    p.add_argument("--detail", default="")
    p.add_argument("--to-fact-seq", type=int)

    p = sub.add_parser("claim-activity", help="lease expensive duplicate-prone work")
    p.add_argument("key")
    p.add_argument("--lease-seconds", type=_positive_seconds, default=600.0)
    p = sub.add_parser("release-activity", help="release an activity lease")
    p.add_argument("key")
    p = sub.add_parser("renew-activity", help="renew an activity owned by this worker")
    p.add_argument("key")
    p.add_argument("--lease-seconds", type=_positive_seconds, default=600.0)
    sub.add_parser("list-activities", help="list live activity leases")

    p = sub.add_parser("claim-resource", help="lease an exclusive shared resource")
    p.add_argument("resource_key")
    p.add_argument("--scope", default="activity")
    p.add_argument("--risk-class", default="")
    p.add_argument("--lease-seconds", type=_positive_seconds, default=600.0)
    p = sub.add_parser("release-resource", help="release an owned resource lease")
    p.add_argument("resource_key")
    sub.add_parser("read-resource-locks", help="read active exclusive resource leases")

    sub.add_parser("read-directives", help="read active operator directives")
    p = sub.add_parser("directive-status", help="read one operator directive")
    p.add_argument("directive_id")
    p = sub.add_parser("doctor", help="validate DB discovery, scope and core schema")
    p.add_argument("--strict", action="store_true", help="require the latest upstream columns")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    global _CLI_DB, _CLI_CHALLENGE_ID, _CLI_ACTOR, _CLI_INTENT_ID
    args = _build_parser().parse_args(argv)
    _CLI_DB = (args.db or "").strip()
    _CLI_CHALLENGE_ID = (args.challenge_id or "").strip()
    _CLI_ACTOR = (args.actor or "").strip()
    _CLI_INTENT_ID = (args.context_intent_id or "").strip()

    try:
        if args.cmd == "read-facts":
            read_facts(args.verified_only, args.limit, args.since_seq)
        elif args.cmd == "read-review":
            read_review()
        elif args.cmd == "read-routes":
            read_routes()
        elif args.cmd == "read-branches":
            read_branches()
        elif args.cmd == "read-deadends":
            read_deadends(args.limit, args.since_seq)
        elif args.cmd == "read-flags":
            read_flags()
        elif args.cmd == "list-intents":
            list_intents()
        elif args.cmd == "write-fact":
            write_fact(args.text, args.verified)
        elif args.cmd == "mark-deadend":
            mark_deadend(args.reason)
        elif args.cmd == "claim":
            claim(args.target_intent_id, args.lease_seconds)
        elif args.cmd == "renew-intent":
            renew_intent(args.target_intent_id, args.lease_seconds)
        elif args.cmd == "release-intent":
            release_intent(args.target_intent_id)
        elif args.cmd == "complete-intent":
            complete_intent(
                args.target_intent_id,
                result=args.result,
                detail=args.detail,
                to_fact_seq=args.to_fact_seq,
            )
        elif args.cmd == "claim-activity":
            claim_activity(args.key, args.lease_seconds)
        elif args.cmd == "release-activity":
            release_activity(args.key)
        elif args.cmd == "renew-activity":
            renew_activity(args.key, args.lease_seconds)
        elif args.cmd == "list-activities":
            list_activities()
        elif args.cmd == "claim-resource":
            claim_resource(
                args.resource_key,
                scope=args.scope,
                risk_class=args.risk_class,
                lease_seconds=args.lease_seconds,
            )
        elif args.cmd == "release-resource":
            release_resource(args.resource_key)
        elif args.cmd == "read-resource-locks":
            read_resource_locks()
        elif args.cmd == "read-directives":
            read_directives()
        elif args.cmd == "directive-status":
            directive_status(args.directive_id)
        elif args.cmd == "doctor":
            return 0 if doctor(args.strict) else 2
        return 0
    except BlackboardError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"ERROR: SQLite failure: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
