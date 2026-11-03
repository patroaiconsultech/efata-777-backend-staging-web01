#!/usr/bin/env python3
"""EFATÀ 777 — Railway recovery migrations, review candidate.

INERT BY DEFAULT: EFATA_RECOVERY_MODE=inspect is read-only.
NEVER use on existing/historical databases. AO-01 review and separate
human approval are REQUIRED before EFATA_RECOVERY_MODE=apply.
No DROP, STAMP, DOWNGRADE, SEED or backend DATABASE_URL changes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import sqlalchemy as sa
from sqlalchemy.engine import make_url

# Import ONLY helpers, manifests and assertions. DO NOT call the CI-only
# preflight() or main(): their guard rails MUST remain untouched.
import schema_recovery_full_chain_ci as lab

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PROJECT_ID = "7411bc43-ea08-40b7-94a8-f38ab3c49523"
EXPECTED_ENV_ID = "e5dec1eb-5d79-4d6d-ad0a-c30443a66ba7"
EXPECTED_TARGET_SERVICE_ID = "6de94970-661e-4080-9c63-17dd41357963"
EXPECTED_TARGET_SERVICE_NAME = "postgres-efata-recovery"
EXPECTED_RUNNER_NAME = "efata-777-recovery-migrations"
EXPECTED_CI_SOURCE_COMMIT = "b4165bb85762d8fd5a414eb846ec13a4cc97ad1b"
EXPECTED_HEAD = "016_thread_participant_access_expiry"
WRITE_PHRASE = "AO01_AND_OWNER_APPROVED_ONCE_001_016"
STOP = "STOP_AND_PRESERVE"


class GateViolation(Exception):
    """Exception whose message is safe to report in evidence logs."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GateViolation(code)


def emit(stage: str, status: str, **details: object) -> None:
    print(json.dumps({"stage": stage, "status": status, **details},
                     sort_keys=True, ensure_ascii=False), flush=True)


def verify_source() -> None:
    """Freeze migrations/env.py/models to the exact CI-tested source hashes."""
    require(not (ROOT / ".env").exists(), "DOTENV_FORBIDDEN")
    require(len(lab.REVISIONS) == 16, "REVISION_COUNT")
    require(lab.REVISION_IDS[lab.REVISIONS[-1]] == EXPECTED_HEAD,
            "EXPECTED_HEAD_MISMATCH")
    require(len(lab.SOURCE_HASHES) == 18, "SOURCE_MANIFEST_COUNT")
    root = ROOT.resolve()
    for name, digest in lab.SOURCE_HASHES.items():
        path = (ROOT / name).resolve()
        require(path.is_relative_to(root) and path.is_file(), "SOURCE_MISSING")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                "SOURCE_HASH_MISMATCH:" + name)
    emit("source", "PASS", baseline_commit=EXPECTED_CI_SOURCE_COMMIT,
         files_verified=len(lab.SOURCE_HASHES),
         runtime_commit=os.getenv("RAILWAY_GIT_COMMIT_SHA", "missing"))


def validate_environment(env: dict[str, str]) -> sa.engine.URL:
    """Service/project and DSN contract; NO network calls and NO logging URLs."""
    require(env.get("RAILWAY_PROJECT_ID") == EXPECTED_PROJECT_ID,
            "RAILWAY_PROJECT_ID_MISMATCH")
    require(env.get("RAILWAY_ENVIRONMENT_ID") == EXPECTED_ENV_ID,
            "RAILWAY_ENVIRONMENT_ID_MISMATCH")
    require(env.get("RAILWAY_ENVIRONMENT_NAME") == "staging-web01",
            "WRONG_RAILWAY_ENVIRONMENT")
    require(env.get("RAILWAY_SERVICE_NAME") == EXPECTED_RUNNER_NAME,
            "NOT_ISOLATED_RUNNER_SERVICE")
    require(bool(env.get("RAILWAY_SERVICE_ID")) and
            env["RAILWAY_SERVICE_ID"] != EXPECTED_TARGET_SERVICE_ID,
            "NOT_SEPARATE_FROM_DATABASE_SERVICE")
    require(bool(re.fullmatch(r"[0-9a-f]{40}",
                              env.get("RAILWAY_GIT_COMMIT_SHA", ""))),
            "SOURCE_GIT_COMMIT_UNVERIFIED")
    require(env.get("EFATA_TARGET_RAILWAY_SERVICE_ID") ==
            EXPECTED_TARGET_SERVICE_ID, "TARGET_SERVICE_ID_MISMATCH")
    require(env.get("EFATA_TARGET_SERVICE_NAME") ==
            EXPECTED_TARGET_SERVICE_NAME, "TARGET_SERVICE_NAME_MISMATCH")
    # Captured separately by the owner inside Railway Database -> Data for
    # the NEW service (read-only pg_control_system query). It is NEVER taken
    # from a variable published by the target service itself.
    cluster_id = env.get("EFATA_EXPECTED_PG_SYSTEM_ID", "")
    require(bool(re.fullmatch(r"[1-9][0-9]{10,24}", cluster_id)),
            "INDEPENDENT_CLUSTER_FINGERPRINT_REQUIRED")
    # Compare GitHub deployment SHA against an AO-01-reviewed expected SHA.
    # Both assertions are configuration gates, not independent signatures.
    approved_sha = env.get("EFATA_EXPECTED_RUNNER_COMMIT", "")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", approved_sha)) and
            approved_sha == env.get("RAILWAY_GIT_COMMIT_SHA"),
            "RUNNER_COMMIT_NOT_APPROVED")
    host = env.get("EFATA_TARGET_PRIVATE_HOST", "").strip().lower()
    require(host.endswith(".railway.internal") and
            host not in {"railway.internal", ".railway.internal"},
            "PRIVATE_HOST_INVALID")
    raw = env.get("DATABASE_URL", "")
    require(bool(raw), "DATABASE_URL_MISSING")
    try:
        # SQLAlchemy handles special characters in the password safely.
        url = make_url(raw)
    except Exception as exc:
        raise GateViolation("DATABASE_URL_UNPARSEABLE") from None
    require(url.drivername in {"postgresql", "postgresql+psycopg"},
            "DATABASE_DRIVER_NOT_POSTGRESQL")
    require(url.host is not None and url.host.lower() == host,
            "DATABASE_HOST_NOT_TARGET_PRIVATE_HOST")
    require(url.database == "railway" and url.port == 5432,
            "DATABASE_IDENTITY_NOT_RAILWAY_5432")
    require(url.username is not None and url.username ==
            env.get("EFATA_TARGET_PGUSER"),
            "DATABASE_USER_NOT_TARGET_SERVICE")
    require(env.get("EFATA_TARGET_PGDATABASE") == "railway",
            "DATABASE_NAME_NOT_TARGET_SERVICE")
    require(bool(url.password), "DATABASE_PASSWORD_MISSING")
    # Never trust a public DSN or allow a remote socket.
    require("proxy.rlwy.net" not in host, "PUBLIC_PROXY_FORBIDDEN")
    require("DATABASE_PUBLIC_URL" not in env,
            "PUBLIC_DATABASE_URL_PRESENT")
    require(url.query.get("sslmode", "require") in ("require", "verify-ca", "verify-full"),
            "TLS_MODE_FORBIDDEN")
    # Force SSL for both this program AND all Alembic subprocesses. No
    # credential/URL will be written to stdout or passed in argv.
    return url.set(drivername="postgresql+psycopg",
                   query={**dict(url.query), "sslmode": "require",
                          "connect_timeout": "8",
                          "application_name": "efata_recovery_001_016"})


def snapshot_connection(conn: sa.Connection) -> dict[str, object]:
    """Only SELECT against pg_catalog; never mutate data or schema."""
    info = conn.execute(sa.text("""
        SELECT current_database(), current_user,
               current_setting('server_version_num')::integer,
               current_schema(), inet_server_port(),
               (SELECT ssl FROM pg_stat_ssl
                WHERE pid = pg_backend_pid()),
               (SELECT system_identifier::text FROM pg_control_system())
    """)).one()
    objects = conn.execute(sa.text("""
        SELECT count(*) FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN
                ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp_%'
          AND c.relkind IN ('r','p','v','m','S','f')
    """)).scalar_one()
    alembic_exists = conn.execute(sa.text(
        "SELECT to_regclass('public.alembic_version') IS NOT NULL"
    )).scalar_one()
    credentials_exists = conn.execute(sa.text(
        "SELECT to_regclass('public.native_credentials') IS NOT NULL"
    )).scalar_one()
    other_sessions = conn.execute(sa.text("""
        SELECT count(*) FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND backend_type = 'client backend'
    """)).scalar_one()
    return {
        "database": info[0], "user": info[1], "pg_version_num": info[2],
        "schema": info[3], "pg_port": info[4], "ssl": bool(info[5]),
        "user_objects": int(objects), "alembic_exists": bool(alembic_exists),
        "credentials_exists": bool(credentials_exists),
        "other_sessions": int(other_sessions),
        "cluster_system_id": str(info[6]),
    }



def snapshot(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as conn:
        # Make the initial runtime inspect read-only at transaction level.
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        return snapshot_connection(conn)


def verify_cluster_identity(state: dict[str, object], expected_id: str) -> None:
    """Fail closed if the independent target cluster fingerprint differs."""
    require(str(state.get("cluster_system_id", "")) == expected_id,
            "DATABASE_CLUSTER_IDENTITY_MISMATCH")


def verify_fresh(state: dict[str, object], expected_user: str,
                 *, enforce_sessions: bool) -> None:
    require(state["database"] == "railway", "CONNECTED_DATABASE_MISMATCH")
    require(state["user"] == expected_user, "CONNECTED_DB_USER_MISMATCH")
    require(160000 <= int(state["pg_version_num"]) < 170000,
            "POSTGRESQL_NOT_16")
    require(state["pg_port"] == 5432, "SERVER_PORT_MISMATCH")
    require(state["schema"] == "public", "SEARCH_PATH_NOT_PUBLIC")
    require(state["ssl"] is True, "POSTGRES_CONNECTION_NOT_TLS")
    require(state["user_objects"] == 0, "TARGET_NOT_EMPTY")
    require(state["alembic_exists"] is False, "ALEMBIC_ALREADY_PRESENT")
    require(state["credentials_exists"] is False, "CREDENTIALS_ALREADY_PRESENT")
    if enforce_sessions:
        require(state["other_sessions"] == 0, "UNEXPECTED_DB_CLIENT_SESSION")


def report_snapshot(state: dict[str, object], stage: str) -> None:
    # Do not print user / DSN / SQL / secrets. The version and table counts
    # are the evidence required by AO-01.
    emit(stage, "OBSERVED", database="railway",
         pg_version_major=int(state["pg_version_num"]) // 10000,
         user_objects=state["user_objects"],
         alembic_exists=state["alembic_exists"],
         credentials_exists=state["credentials_exists"],
         other_client_sessions=state["other_sessions"], ssl=state["ssl"])


def evidence_on_failure(engine: sa.Engine) -> None:
    """Bounded read-only catalogue of potentially partially applied schema."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("""
                SELECT tablename FROM pg_catalog.pg_tables
                WHERE schemaname = 'public'
            """)).scalars().all()
            has_version = "alembic_version" in rows
            head = None
            if has_version:
                versions = conn.execute(sa.text(
                    "SELECT version_num FROM public.alembic_version LIMIT 2"
                )).scalars().all()
                head = versions[0] if len(versions) == 1 else "INVALID"
            emit("failure_catalog", "PRESERVED", table_count=len(rows),
                 alembic_head=head, native_credentials="native_credentials" in rows)
    except Exception as exc:
        emit("failure_catalog", "UNAVAILABLE", error_type=type(exc).__name__)


def apply_once(engine: sa.Engine, url: sa.engine.URL) -> None:
    """Requires independent approval; never retries from a partial state."""
    lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        locked = lock_conn.execute(
            sa.text("SELECT pg_try_advisory_lock(777, 16)")
        ).scalar_one()
        require(locked is True, "MIGRATION_LOCK_BUSY")
        # Recheck fresh state AFTER taking the lock, directly before DDL.
        state = snapshot_connection(lock_conn)
        report_snapshot(state, "write_preflight")
        verify_fresh(state, url.username or "", enforce_sessions=True)
        verify_cluster_identity(
            state, os.environ["EFATA_EXPECTED_PG_SYSTEM_ID"]
        )
        # Alembic reads DATABASE_URL via get_settings(); the normalized URL
        # was installed by main(), and is not passed via shell arguments.
        for index, filename in enumerate(lab.REVISIONS, 1):
            rev = lab.REVISION_IDS[filename]
            emit("migration", "START", revision=rev, ordinal=index)
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", rev],
                    cwd=ROOT, env=os.environ.copy(), capture_output=True,
                    timeout=240, check=False,
                )
            except subprocess.TimeoutExpired:
                raise GateViolation("ALEMBIC_TIMEOUT:" + rev) from None
            require(result.returncode == 0, "ALEMBIC_FAILED:" + rev)
            # Reuse exact same post-checks that were used in CI lab.
            lab.state(engine, rev)
            if index >= 12:
                lab.normalized_email_index(engine)
            if index >= 14:
                lab.columns_014_016(engine, index)
            emit("migration", "PASS", revision=rev, ordinal=index)
        lab.final_shape(engine)
        with engine.connect() as conn:
            total = conn.execute(sa.text("""
                SELECT count(*) FROM pg_catalog.pg_tables
                WHERE schemaname='public'
            """)).scalar_one()
        require(total == 39, "FINAL_TABLE_COUNT_NOT_39")
        emit("full_chain", "PASS", migrations=16, public_tables=39,
             head=EXPECTED_HEAD)
    finally:
        try:
            lock_conn.execute(sa.text("SELECT pg_advisory_unlock(777, 16)"))
        except Exception:
            pass
        lock_conn.close()


def main() -> int:
    engine = None
    started_writing = False
    try:
        mode = os.getenv("EFATA_RECOVERY_MODE", "inspect")
        require(mode in {"inspect", "apply"}, "INVALID_MODE")
        # Never accidentally migrate on the initial creation/deploy.
        if mode == "apply":
            require(os.getenv("EFATA_RECOVERY_WRITE_APPROVAL") == WRITE_PHRASE,
                    "WRITE_NOT_APPROVED")
            require(os.getenv("EFATA_RECOVERY_RUN_ONCE") == "enabled",
                    "RUN_ONCE_NOT_ENABLED")
        verify_source()
        normalized = validate_environment(dict(os.environ))
        # This is the ONLY database URL Alembic may read. No URLs in logs.
        os.environ["DATABASE_URL"] = normalized.render_as_string(hide_password=False)
        os.environ["PGSSLMODE"] = "require"
        os.environ["PGOPTIONS"] = "-c search_path=public -c lock_timeout=10000 -c statement_timeout=180000"
        from orkio_v2.config import get_settings
        from orkio_v2.database import normalize_database_url
        require(normalize_database_url(get_settings().database_url) ==
                os.environ["DATABASE_URL"], "ALEMBIC_EFFECTIVE_TARGET_MISMATCH")
        engine = sa.create_engine(normalized, pool_pre_ping=True,
                                  pool_size=1, max_overflow=2)
        observed = snapshot(engine)
        report_snapshot(observed, "preflight")
        verify_fresh(observed, normalized.username or "",
                     enforce_sessions=(mode == "apply"))
        verify_cluster_identity(
            observed, os.environ["EFATA_EXPECTED_PG_SYSTEM_ID"]
        )
        emit("preflight", "PASS", mode=mode,
             target_service_id=EXPECTED_TARGET_SERVICE_ID,
             independent_cluster_identity_match=True)
        if mode == "inspect":
            emit("job", "INSPECT_PASS_NO_WRITES", migration_executed=False)
            return 0
        # A single try only. After a partial migration, the empty-database
        # preflight blocks all further attempts automatically.
        started_writing = True
        apply_once(engine, normalized)
        emit("job", "MIGRATION_PASS", migration_executed=True)
        return 0
    except GateViolation as exc:
        emit("job", STOP, reason=str(exc), write_phase_entered=started_writing)
        if started_writing and engine is not None:
            evidence_on_failure(engine)
        return 2
    except Exception as exc:
        # NEVER print raw DB/driver/Alembic exceptions: may contain DSNs.
        emit("job", STOP, reason="UNEXPECTED_EXCEPTION",
             exception_type=type(exc).__name__,
             write_phase_entered=started_writing)
        if started_writing and engine is not None:
            evidence_on_failure(engine)
        return 3
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
