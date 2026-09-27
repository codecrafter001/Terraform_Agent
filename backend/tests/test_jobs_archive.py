"""Job archiving (soft delete) and the Migration Safety result kept on the
audit record for the dashboard. Uses the in-memory SQLite database from
conftest and calls the route functions directly, so no Redis is needed."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text

import services.database as database
from routers.jobs import archive_job, list_jobs
from services.database import (
    archive_job_record,
    create_job_record,
    get_job_record,
    init_db,
    list_job_records,
    mark_job_complete,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _new_job(status: str = "COMPLETE") -> str:
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    create_job_record(job_id, "generate", "us-east-1", "2026-09-26T10:00:00")
    if status != "RUNNING":
        mark_job_complete(job_id, {"status": status})
    return job_id


def _listed_ids(include_archived: bool = False) -> set:
    return {r.job_id for r in list_job_records(limit=100, include_archived=include_archived)}


def test_mark_job_complete_stores_migration_safety():
    job_id = _new_job()
    mark_job_complete(job_id, {"status": "COMPLETE", "migration_safety": {"score": 97, "status": "CHANGES"}})
    record = get_job_record(job_id)
    assert record.migration_safety_score == 97
    assert record.migration_safety_status == "CHANGES"


def test_migration_safety_without_evidence_stays_null_never_100():
    job_id = _new_job()
    mark_job_complete(job_id, {"status": "COMPLETE", "migration_safety": {"score": None, "status": "UNVERIFIED"}})
    record = get_job_record(job_id)
    assert record.migration_safety_score is None
    assert record.migration_safety_status == "UNVERIFIED"

    other = _new_job()  # no scores at all (e.g. an older pipeline state)
    assert get_job_record(other).migration_safety_score is None
    assert get_job_record(other).migration_safety_status is None


def test_archived_jobs_are_hidden_by_default_but_kept():
    kept, archived = _new_job(), _new_job()
    assert archive_job_record(archived) is True

    assert kept in _listed_ids()
    assert archived not in _listed_ids()
    assert archived in _listed_ids(include_archived=True)
    # Soft delete: the audit record is still there.
    assert get_job_record(archived) is not None


def test_rows_from_before_the_archived_column_are_listed():
    job_id = _new_job()
    with database.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET archived = NULL WHERE job_id = :id"), {"id": job_id})
    assert job_id in _listed_ids()


def test_archive_unknown_job_is_404():
    assert archive_job_record("job-doesnotexist") is False
    with pytest.raises(HTTPException) as exc:
        asyncio.run(archive_job("job-doesnotexist"))
    assert exc.value.status_code == 404


@pytest.mark.parametrize("status", ["RUNNING", "AWAITING_APPROVAL"])
def test_unfinished_jobs_cannot_be_archived(status):
    job_id = _new_job(status)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(archive_job(job_id))
    assert exc.value.status_code == 409
    assert job_id in _listed_ids()


@pytest.mark.parametrize("status", ["COMPLETE", "FAILED", "REJECTED"])
def test_finished_jobs_can_be_archived_through_the_route(status):
    job_id = _new_job(status)
    assert asyncio.run(archive_job(job_id)) == {"job_id": job_id, "archived": True}

    listed = {r.job_id for r in asyncio.run(list_jobs(limit=100, offset=0, include_archived=False, operation=None))}
    assert job_id not in listed
    everything = asyncio.run(list_jobs(limit=100, offset=0, include_archived=True, operation=None))
    record = next(r for r in everything if r.job_id == job_id)
    assert record.archived is True


def test_init_db_adds_the_new_columns_to_an_existing_jobs_table(tmp_path, monkeypatch):
    """A persistent Postgres volume already has a jobs table without these
    columns; init_db must add them or every write would fail."""
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.execute(text(
            "CREATE TABLE jobs (job_id VARCHAR PRIMARY KEY, operation VARCHAR NOT NULL, "
            "region VARCHAR NOT NULL, status VARCHAR NOT NULL, created_at VARCHAR NOT NULL)"
        ))
    monkeypatch.setattr(database, "engine", old)
    init_db()
    columns = {c["name"] for c in inspect(old).get_columns("jobs")}
    assert {"archived", "migration_safety_score", "migration_safety_status"} <= columns
    old.dispose()
