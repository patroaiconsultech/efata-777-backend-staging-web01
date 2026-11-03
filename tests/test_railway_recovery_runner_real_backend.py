"""Recovery runner gates with the REAL CI helper and frozen backend sources.

Run inside the complete pinned backend tree, not as a stand-alone bundle.
No Railway network or PostgreSQL access is attempted.
"""
from __future__ import annotations

import io
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import schema_recovery_full_chain_ci as lab  # REAL file; never simulated!

if not hasattr(lab, "SOURCE_HASHES") or len(lab.SOURCE_HASHES) != 18:
    raise RuntimeError("REAL_CI_HELPER_REQUIRED")

spec = importlib.util.spec_from_file_location(
    "railway_recovery_qualified", ROOT / "scripts/railway_recovery_migrate_once.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def valid_env() -> dict[str, str]:
    return {
        "RAILWAY_PROJECT_ID": runner.EXPECTED_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": runner.EXPECTED_ENV_ID,
        "RAILWAY_ENVIRONMENT_NAME": "staging-web01",
        "RAILWAY_SERVICE_NAME": runner.EXPECTED_RUNNER_NAME,
        "RAILWAY_SERVICE_ID": "11111111-2222-3333-4444-555555555555",
        "RAILWAY_GIT_COMMIT_SHA": "a" * 40,
        "EFATA_EXPECTED_RUNNER_COMMIT": "a" * 40,
        "EFATA_TARGET_RAILWAY_SERVICE_ID": runner.EXPECTED_TARGET_SERVICE_ID,
        "EFATA_TARGET_SERVICE_NAME": runner.EXPECTED_TARGET_SERVICE_NAME,
        "EFATA_TARGET_PRIVATE_HOST": "postgres-efata-recovery.railway.internal",
        "EFATA_TARGET_PGUSER": "postgres",
        "EFATA_TARGET_PGDATABASE": "railway",
        "EFATA_EXPECTED_PG_SYSTEM_ID": "1234567890123456789",
        "DATABASE_URL": "postgresql://postgres:fake_for_tests_only@postgres-efata-recovery.railway.internal:5432/railway",
    }


def valid_state() -> dict[str, object]:
    return {
        "database": "railway", "user": "postgres", "pg_version_num": 160015,
        "schema": "public", "pg_port": 5432, "ssl": True,
        "user_objects": 0, "alembic_exists": False,
        "credentials_exists": False, "other_sessions": 0,
        "cluster_system_id": "1234567890123456789",
    }


class RecoveryQualificationTests(unittest.TestCase):
    def test_real_helper_source_manifest_matches_all_18_files(self):
        self.assertEqual(len(lab.REVISIONS), 16)
        self.assertEqual(len(lab.SOURCE_HASHES), 18)
        # FULL, unmodified migration chain; no stubs or env-dependent network.
        runner.verify_source()

    def test_real_helper_postcheck_functions_exist(self):
        for name in ("state", "normalized_email_index", "columns_014_016", "final_shape"):
            self.assertTrue(callable(getattr(lab, name)))

    def test_valid_private_target(self):
        url = runner.validate_environment(valid_env())
        self.assertEqual(url.host, "postgres-efata-recovery.railway.internal")
        self.assertEqual(url.query["sslmode"], "require")

    def test_not_postgresql_16_blocks(self):
        state = valid_state()
        state["pg_version_num"] = 170002
        with self.assertRaisesRegex(runner.GateViolation, "POSTGRESQL_NOT_16"):
            runner.verify_fresh(state, "postgres", enforce_sessions=False)

    def test_target_service_id_wrong_blocks(self):
        env = valid_env()
        env["EFATA_TARGET_RAILWAY_SERVICE_ID"] = "historical-postgres-id"
        with self.assertRaisesRegex(runner.GateViolation, "TARGET_SERVICE_ID"):
            runner.validate_environment(env)

    def test_wrong_postgres_host_blocks(self):
        env = valid_env()
        env["DATABASE_URL"] = env["DATABASE_URL"].replace(
            "postgres-efata-recovery.railway.internal",
            "postgres-web01.railway.internal",
        )
        with self.assertRaisesRegex(runner.GateViolation, "DATABASE_HOST_NOT_TARGET"):
            runner.validate_environment(env)

    def test_public_proxy_blocks(self):
        env = valid_env()
        env["DATABASE_URL"] = (
            "postgresql://postgres:fake@something.proxy.rlwy.net:18182/railway"
        )
        with self.assertRaises(runner.GateViolation):
            runner.validate_environment(env)

    def test_runner_git_sha_mismatch_blocks(self):
        env = valid_env()
        env["RAILWAY_GIT_COMMIT_SHA"] = "b" * 40
        with self.assertRaisesRegex(runner.GateViolation, "RUNNER_COMMIT_NOT_APPROVED"):
            runner.validate_environment(env)

    def test_independent_cluster_fingerprint_required(self):
        env = valid_env()
        env["EFATA_EXPECTED_PG_SYSTEM_ID"] = ""
        with self.assertRaisesRegex(
            runner.GateViolation, "INDEPENDENT_CLUSTER_FINGERPRINT_REQUIRED"
        ):
            runner.validate_environment(env)

    def test_mismatching_postgres_cluster_id_blocks(self):
        state = valid_state()
        state["cluster_system_id"] = "9999999999999999999"
        with self.assertRaisesRegex(runner.GateViolation, "DATABASE_CLUSTER_IDENTITY"):
            runner.verify_cluster_identity(
                state, valid_env()["EFATA_EXPECTED_PG_SYSTEM_ID"]
            )

    def test_matching_postgres_cluster_id_passes(self):
        runner.verify_cluster_identity(
            valid_state(), valid_env()["EFATA_EXPECTED_PG_SYSTEM_ID"]
        )

    def test_nonempty_schema_blocks(self):
        state = valid_state()
        state["user_objects"] = 1
        with self.assertRaisesRegex(runner.GateViolation, "TARGET_NOT_EMPTY"):
            runner.verify_fresh(state, "postgres", enforce_sessions=False)

    def test_non_tls_blocks(self):
        state = valid_state()
        state["ssl"] = False
        with self.assertRaisesRegex(runner.GateViolation, "NOT_TLS"):
            runner.verify_fresh(state, "postgres", enforce_sessions=False)

    def test_other_client_session_blocks_apply(self):
        state = valid_state()
        state["other_sessions"] = 1
        with self.assertRaisesRegex(runner.GateViolation, "UNEXPECTED_DB_CLIENT"):
            runner.verify_fresh(state, "postgres", enforce_sessions=True)

    def test_inspect_uses_readonly_transaction(self):
        engine = MagicMock()
        conn = engine.connect.return_value.__enter__.return_value
        with patch.object(runner, "snapshot_connection", return_value=valid_state()):
            got = runner.snapshot(engine)
        self.assertEqual(got["database"], "railway")
        conn.exec_driver_sql.assert_called_once_with("SET TRANSACTION READ ONLY")

    def test_write_gate_blocks_before_database_or_source(self):
        with patch.dict(os.environ, {"EFATA_RECOVERY_MODE": "apply"}, clear=True):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = runner.main()
        self.assertEqual(exit_code, 2)
        self.assertIn("WRITE_NOT_APPROVED", output.getvalue())

    def test_dockerfile_is_inspect_default(self):
        text = (ROOT / "Dockerfile.recovery-qualification").read_text()
        self.assertIn("COPY migrations ./migrations", text)
        self.assertIn("COPY scripts ./scripts", text)
        self.assertIn("COPY src ./src", text)
        self.assertIn("requirements.lock.txt", text)
        self.assertIn("railway_recovery_migrate_once.py", text)
        self.assertNotIn("EFATA_RECOVERY_MODE=apply", text)
        self.assertNotIn("alembic upgrade", text)


if __name__ == "__main__":
    unittest.main()
