from plannerboard import ladder


def test_entry_task_and_quote_task(store):
    pid = store.create_project("Quote job", work_type="repair")
    st = ladder.ensure_entry_stage_task(store, pid)
    assert st
    row = next(s for s in store.open_subtasks(pid) if s["id"] == st)
    assert row["text"] == "Prepare and send proposal" and row["date_type"] == "quote" and row["sla_created"]
    assert ladder.ensure_entry_stage_task(store, pid) == st          # idempotent
    assert ladder.is_quote_task(store, row, store.get_project(pid))
    svc = store.create_project("Service", work_type="service_call")
    st2 = ladder.ensure_entry_stage_task(store, svc)
    row2 = next(s for s in store.open_subtasks(svc) if s["id"] == st2)
    assert row2["text"] == "Forward to dispatch" and row2["sla_type"] == "response" and row2["sla_hours"] == 4
    gen = store.create_project("Task", work_type="task")
    st3 = ladder.ensure_entry_stage_task(store, gen)
    assert next(s for s in store.open_subtasks(gen) if s["id"] == st3)["text"] == "Respond"


def test_ladder_advance_and_revert(store):
    pid = store.create_project("Sold job", work_type="repair", status="job_setup")
    sid = ladder.ensure_ladder_task(store, pid, "job_setup")
    row = next(s for s in store.open_subtasks(pid) if s["id"] == sid)
    assert row["text"] == "Set up job" and row["sla_hours"] == 4 and row["sla_type"] == "response"
    assert ladder.ensure_ladder_task(store, pid, "job_setup") == sid   # find, not duplicate
    store.complete_subtask(pid, sid, completed_by="manual")
    assert ladder.advance_job_ladder(store, pid, "job_setup") == "parts_needed"
    p = store.get_project(pid)
    assert p["status"] == "parts_needed"
    assert [s["text"] for s in store.open_subtasks(pid)] == ["Order parts"]
    ladder.revert_job_ladder(store, pid, "job_setup")
    assert store.get_project(pid)["status"] == "job_setup"
    # walk to the end: ready_to_schedule -> scheduled has no task
    store.update_project(pid, status="ready_to_schedule")
    tid = ladder.ensure_ladder_task(store, pid, "ready_to_schedule")
    trow = next(s for s in store.open_subtasks(pid) if s["id"] == tid)
    assert trow["text"] == "Schedule job" and trow["sla_type"] is None and trow["follow_up_date"]
    assert ladder.advance_job_ladder(store, pid, "ready_to_schedule") == "scheduled"
    assert ladder.advance_job_ladder(store, pid, "scheduled") is None


def test_rearm_on_status_change(store):
    pid = store.create_project("Pulled back", work_type="repair", status="parts_needed")
    ladder.ensure_ladder_task(store, pid, "parts_needed")
    before = store.get_project(pid)
    store.update_project(pid, status="job_setup")
    ladder.rearm_on_status_change(store, pid, before, "job_setup")
    texts = [s["text"] for s in store.open_subtasks(pid)]
    assert texts == ["Set up job"]                     # stale rung task dropped, right one armed
    before = store.get_project(pid)
    store.update_project(pid, status="complete")
    ladder.rearm_on_status_change(store, pid, before, "complete")
    assert store.open_subtasks(pid) == []
    assert any(s["status"] == "complete" for s in store.get_subtasks(pid))   # audit row kept


def test_spawn_response_subtask_sla_math(store):
    pid = store.create_project("SLA", work_type="task")
    inbound = {"from_email": "ann@client.com", "from_name": "Ann", "subject": "Need help",
               "received_at": "2026-10-02T21:30:00Z", "to": "pat@example.com"}
    sid = ladder.spawn_response_subtask(store, inbound, pid, from_is_user=False)
    row = next(s for s in store.open_subtasks(pid) if s["id"] == sid)
    assert row["text"] == "Respond to Ann: Need help"
    assert row["sla_type"] == "response" and row["sla_hours"] == 4 and row["date_type"] == "sla"
    # 21:30Z on a Friday is 17:30 in the configured zone -> clock reads local
    assert row["sla_created"].startswith("2026-10-02T17:30:00")
    # elapsed by Monday 10:00 local = 2h (Friday after close counts nothing)
    assert store.hours.elapsed_biz_hours(row["sla_created"], "2026-10-05T10:00:00") == 2.0
    out = {"from_email": "pat@example.com", "from_name": "Pat Example", "subject": "Re: Need help",
           "received_at": "2026-10-05T14:00:00Z", "to": "ann@client.com; bob@client.com"}
    fid = ladder.spawn_response_subtask(store, out, pid, from_is_user=True)
    frow = next(s for s in store.open_subtasks(pid) if s["id"] == fid)
    assert frow["text"].startswith("Follow up with ann@client.com") and frow["sla_type"] is None
    assert frow["follow_up_date"]
    robot = {"from_email": "no-reply@shop.example", "from_name": "Shop", "subject": "Your order has shipped",
             "received_at": "2026-10-05T14:00:00Z"}
    assert ladder.spawn_response_subtask(store, robot, pid, from_is_user=False) is None
    # the user's reply clears the SLA it answers
    store.auto_complete_response_subtasks(pid, dict(out, received_at="2026-10-05T16:00:00Z"))
    assert not any(s["text"].startswith("Respond to") for s in store.open_subtasks(pid))
