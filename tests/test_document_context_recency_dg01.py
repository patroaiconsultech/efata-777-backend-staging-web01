"""DG-01: only the newest authorized thread attachments consume document context slots.

These tests use only in-memory SQLite for deterministic selection ordering; PostgreSQL
runtime validation remains a separate deployment gate.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from orkio_v2.config import Settings
from orkio_v2.database import Base
from orkio_v2.models import Attachment
from orkio_v2.services.document_context import build_document_context


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db
    engine.dispose()


@pytest.fixture
def settings(tmp_path):
    config = Settings(_env_file=None)
    config.document_context_enabled = True
    config.document_context_max_files = 6
    config.document_context_max_chars = 48_000
    config.document_context_max_chars_per_file = 20_000
    config.artifact_storage_path = str(tmp_path)
    return config


def _insert(session, settings, *, aid, tenant="tenant-a", thread="thread-a", when, text=None):
    data = (text or f"SENTINEL-{aid}").encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    key = f"{tenant}/{thread}/{aid}.txt"
    target = Path(settings.artifact_storage_path) / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    session.add(
        Attachment(
            id=aid,
            tenant_id=tenant,
            thread_id=thread,
            uploaded_by="user-a",
            filename=f"{aid}.txt",
            mime_type="text/plain",
            size_bytes=len(data),
            sha256=sha,
            storage_key=key,
            status="ready",
            created_at=when,
        )
    )


def test_newest_six_selected_when_thread_has_ten_attachments(session, settings):
    base = datetime(2026, 11, 3, tzinfo=timezone.utc)
    for i in range(10):
        _insert(session, settings, aid=f"att-{i}", when=base + timedelta(minutes=i))
    session.commit()

    bundle = build_document_context(
        session, settings=settings, tenant_id="tenant-a", thread_id="thread-a"
    )
    assert bundle is not None
    assert bundle.provenance.sources == 6
    assert bundle.provenance.source_ids == tuple(f"att-{i}" for i in range(9, 3, -1))
    assert "SENTINEL-att-9" in bundle.message["content"]
    assert "SENTINEL-att-0" not in bundle.message["content"]


def test_newest_document_receives_aggregate_budget_before_older_documents(session, settings):
    settings.document_context_max_files = 3
    settings.document_context_max_chars = 12
    settings.document_context_max_chars_per_file = 20
    base = datetime(2026, 11, 3, tzinfo=timezone.utc)
    _insert(session, settings, aid="old", when=base, text="O" * 10)
    _insert(session, settings, aid="middle", when=base + timedelta(seconds=1), text="M" * 10)
    _insert(session, settings, aid="new", when=base + timedelta(seconds=2), text="N" * 10)
    session.commit()

    bundle = build_document_context(
        session, settings=settings, tenant_id="tenant-a", thread_id="thread-a"
    )
    assert bundle is not None
    assert bundle.provenance.source_ids == ("new", "middle", "old")
    assert [source.provided_chars for source in bundle.provenance.source_provenance] == [10, 2, 0]
    assert bundle.provenance.provided_chars == 12
    assert bundle.provenance.aggregate_truncated is True
    assert "N" * 10 in bundle.message["content"]
    assert "O" * 10 not in bundle.message["content"]


def test_newest_selection_preserves_tenant_thread_visibility_and_tie_break(session, settings):
    settings.document_context_max_files = 2
    base = datetime(2026, 11, 3, tzinfo=timezone.utc)
    _insert(session, settings, aid="own-a", when=base)
    _insert(session, settings, aid="own-b", when=base)
    _insert(session, settings, aid="own-new", when=base + timedelta(seconds=10))
    _insert(session, settings, aid="other-tenant", tenant="tenant-b",
            when=base + timedelta(seconds=30))
    _insert(session, settings, aid="other-thread", thread="thread-b",
            when=base + timedelta(seconds=40))
    session.commit()

    bundle = build_document_context(
        session, settings=settings, tenant_id="tenant-a", thread_id="thread-a",
        visible_since=base,
    )
    assert bundle is not None
    assert bundle.provenance.source_ids == ("own-new", "own-b")
    assert all(s.attachment_id.startswith("own-") for s in bundle.provenance.source_provenance)
    assert "SENTINEL-other-tenant" not in bundle.message["content"]
    assert "SENTINEL-other-thread" not in bundle.message["content"]

    future = build_document_context(
        session, settings=settings, tenant_id="tenant-a", thread_id="thread-a",
        visible_since=base + timedelta(days=1),
    )
    assert future is None
