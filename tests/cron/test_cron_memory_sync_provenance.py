"""run_job stashes job provenance on the cron agent for its memory-sync header.

``agent/cron_memory_sync.py`` builds ``Scheduled job "<name>" (<id>) ran at <iso>`` from
``agent._cron_job_meta``; the stash must name the job without ever carrying its prompt (an unnamed
job's display name falls back to the prompt elsewhere in the scheduler).
"""

from __future__ import annotations

from datetime import datetime

from tests.cron.test_cron_memory_contract import _run_job_patches
from tests.cron.test_scheduler import _run_owned_job


def _meta_for(job, tmp_path):
    with _run_job_patches(tmp_path) as (fake_db, agent_cls):
        _run_owned_job(job, tmp_path, fake_db)
    return agent_cls.return_value._cron_job_meta


def test_run_job_stashes_named_job_provenance(tmp_path):
    meta = _meta_for({"id": "prov-named", "name": "Morning pulse", "prompt": "SENTINEL-PROMPT do x"}, tmp_path)
    assert set(meta) == {"id", "name", "scheduled_at"}
    assert meta["id"] == "prov-named" and meta["name"] == "Morning pulse"
    assert datetime.fromisoformat(meta["scheduled_at"]).tzinfo is not None


def test_unnamed_job_provenance_never_carries_prompt(tmp_path):
    meta = _meta_for({"id": "prov-unnamed", "prompt": "SENTINEL-PROMPT do x"}, tmp_path)
    assert meta["name"] == "prov-unnamed"
    assert "SENTINEL-PROMPT" not in repr(meta)
