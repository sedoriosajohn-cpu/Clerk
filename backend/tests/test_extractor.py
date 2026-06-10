"""Unit tests for extractor.py utilities.

Run with:  python -m pytest Clerk/backend/tests/test_extractor.py -v
Or from the project root: pytest Clerk/backend/tests/ -v
"""
import sys
import os
# Make the app package importable without installing it.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime

from app.extractor import (
    adjust_confidence,
    clamp_score,
    clean_task_title,
    compact_text_for_extraction,
    due_day_key_from_iso,
    evidence_window,
    extract_json,
    local_nlp_extract_tasks,
    normalize_schedule_name,
    parse_current_time,
    parse_due_date,
    parse_task_datetime,
    parse_time_fragment,
    schedule_names_match,
    title_terms,
    validate_task,
    verify_with_regex,
)


# ─── clamp_score ─────────────────────────────────────────────────────────────

def test_clamp_score_in_range():
    assert clamp_score(75.4) == 75

def test_clamp_score_below_zero():
    assert clamp_score(-5) == 0

def test_clamp_score_above_hundred():
    assert clamp_score(120) == 100

def test_clamp_score_boundary():
    assert clamp_score(0) == 0
    assert clamp_score(100) == 100


# ─── parse_task_datetime ─────────────────────────────────────────────────────

def test_parse_task_datetime_iso():
    dt = parse_task_datetime("2026-06-15T14:30:00Z")
    assert dt == datetime(2026, 6, 15, 14, 30, 0)

def test_parse_task_datetime_with_offset():
    dt = parse_task_datetime("2026-06-15T14:30:00+05:00")
    assert dt == datetime(2026, 6, 15, 14, 30, 0)

def test_parse_task_datetime_none():
    assert parse_task_datetime(None) is None

def test_parse_task_datetime_invalid():
    assert parse_task_datetime("not-a-date") is None


# ─── due_day_key_from_iso ─────────────────────────────────────────────────────

def test_due_day_key_from_iso():
    assert due_day_key_from_iso("2026-06-15T10:00:00Z") == "2026-06-15"

def test_due_day_key_from_iso_no_date():
    assert due_day_key_from_iso(None) == ""

def test_due_day_key_from_iso_garbage():
    assert due_day_key_from_iso("garbage") == ""


# ─── title_terms ─────────────────────────────────────────────────────────────

def test_title_terms_filters_stop_words():
    # "the", "new", "assignment" are in the stop_words set; only "submit" survives.
    terms = title_terms("Submit the new assignment")
    assert "the" not in terms
    assert "new" not in terms
    assert "submit" in terms

def test_title_terms_empty():
    assert title_terms("") == []

def test_title_terms_short_words_removed():
    terms = title_terms("Do it now")
    assert "it" not in terms  # too short (< 3 chars)


# ─── clean_task_title ────────────────────────────────────────────────────────

def test_clean_task_title_strips_due():
    assert "due" not in clean_task_title("Finish report due Friday").lower()

def test_clean_task_title_strips_remind_me():
    title = clean_task_title("Remind me to call Mom")
    assert "remind me to" not in title.lower()

def test_clean_task_title_capitalizes():
    title = clean_task_title("buy groceries")
    assert title[0].isupper()

def test_clean_task_title_empty_fallback():
    assert clean_task_title("") == "New Task"


# ─── parse_time_fragment ──────────────────────────────────────────────────────

def test_parse_time_fragment_pm():
    h, m, all_day = parse_time_fragment("at 3pm")
    assert h == 15 and m == 0 and not all_day

def test_parse_time_fragment_noon():
    h, m, all_day = parse_time_fragment("at 12pm")
    assert h == 12 and not all_day

def test_parse_time_fragment_midnight():
    h, m, all_day = parse_time_fragment("at 12am")
    assert h == 0 and not all_day

def test_parse_time_fragment_no_time():
    _, _, all_day = parse_time_fragment("sometime next week")
    assert all_day


# ─── parse_due_date ───────────────────────────────────────────────────────────

NOW = datetime(2026, 6, 10, 9, 0, 0)

def test_parse_due_date_today():
    due, _, _ = parse_due_date("finish today", NOW)
    assert due and "2026-06-10" in due

def test_parse_due_date_tomorrow():
    due, _, _ = parse_due_date("do it tomorrow", NOW)
    assert due and "2026-06-11" in due

def test_parse_due_date_numeric():
    due, _, _ = parse_due_date("due 07/15", NOW)
    assert due and "2026-07-15" in due

def test_parse_due_date_month_name():
    due, _, _ = parse_due_date("submit by August 5", NOW)
    assert due and "2026-08-05" in due

def test_parse_due_date_no_date():
    due, _, all_day = parse_due_date("no date here at all", NOW)
    assert due is None and all_day


# ─── extract_json ─────────────────────────────────────────────────────────────

def test_extract_json_plain_list():
    result = extract_json('[{"title": "Buy milk"}]')
    assert result == [{"title": "Buy milk"}]

def test_extract_json_with_code_fence():
    result = extract_json('```json\n[{"title": "Task"}]\n```')
    assert result == [{"title": "Task"}]

def test_extract_json_single_object():
    result = extract_json('{"title": "Single task"}')
    assert result == [{"title": "Single task"}]

def test_extract_json_invalid_raises():
    with pytest.raises((ValueError, Exception)):
        extract_json("this is not json at all, no braces")


# ─── normalize_schedule_name ─────────────────────────────────────────────────

def test_normalize_schedule_name_last_first():
    assert normalize_schedule_name("Smith, John") == "john smith"

def test_normalize_schedule_name_plain():
    assert normalize_schedule_name("Jane Doe") == "jane doe"

def test_normalize_schedule_name_empty():
    assert normalize_schedule_name(None) == ""


# ─── schedule_names_match ─────────────────────────────────────────────────────

def test_schedule_names_match_exact():
    assert schedule_names_match("John Smith", ["John Smith"])

def test_schedule_names_match_reversed():
    assert schedule_names_match("Smith, John", ["John Smith"])

def test_schedule_names_match_empty_row():
    assert schedule_names_match("", ["John Smith"])

def test_schedule_names_match_no_match():
    assert not schedule_names_match("Alice Jones", ["John Smith"])


# ─── verify_with_regex ────────────────────────────────────────────────────────

def test_verify_with_regex_date_in_text():
    assert verify_with_regex("Submit by 2026-06-15", "2026-06-15T10:00:00Z")

def test_verify_with_regex_month_name_in_text():
    assert verify_with_regex("due June 15", "2026-06-15T00:00:00Z")

def test_verify_with_regex_not_found():
    assert not verify_with_regex("no date here", "2026-06-15T00:00:00Z")

def test_verify_with_regex_no_extracted_date():
    assert verify_with_regex("some text", None)


# ─── validate_task ────────────────────────────────────────────────────────────

def test_validate_task_minimal():
    task = validate_task({"title": "Test task"})
    assert task["item_type"] in ("task", "reminder")
    assert task["priority"] in ("low", "normal", "high")
    assert isinstance(task["confidence"], (int, float))

def test_validate_task_normalises_priority_urgent():
    task = validate_task({"title": "Urgent thing", "priority": "urgent"})
    assert task["priority"] == "high"

def test_validate_task_passes_through_confidence():
    # validate_task normalises schema but does not clamp confidence — that's adjust_confidence's job.
    task = validate_task({"title": "X", "confidence": 85})
    assert isinstance(task["confidence"], (int, float))


# ─── adjust_confidence ───────────────────────────────────────────────────────

def test_adjust_confidence_authoritative_source_calendar():
    task = {"title": "Team standup", "confidence": 50}
    result = adjust_confidence("", task, source_type="calendar:abc")
    assert result["confidence"] >= 95

def test_adjust_confidence_authoritative_source_gtask():
    task = {"title": "Buy milk", "confidence": 50}
    result = adjust_confidence("", task, source_type="gtask:xyz")
    assert result["confidence"] >= 95

def test_adjust_confidence_user_feedback_positive():
    task = {"title": "Submit report", "confidence": 70, "due_date": "2026-06-15T10:00:00Z"}
    baseline = adjust_confidence("Submit report by June 15", dict(task))["confidence"]
    boosted = adjust_confidence(
        "Submit report by June 15", dict(task), user_feedback=1
    )["confidence"]
    assert boosted >= baseline

def test_adjust_confidence_user_feedback_negative():
    task = {"title": "Submit report", "confidence": 70, "due_date": "2026-06-15T10:00:00Z"}
    baseline = adjust_confidence("Submit report by June 15", dict(task))["confidence"]
    penalised = adjust_confidence(
        "Submit report by June 15", dict(task), user_feedback=-1
    )["confidence"]
    assert penalised <= baseline

def test_adjust_confidence_clamps_result():
    task = {"title": "X", "confidence": 0}
    result = adjust_confidence("", task, user_feedback=-1)
    assert 0 <= result["confidence"] <= 100


# ─── local_nlp_extract_tasks ─────────────────────────────────────────────────

def test_local_nlp_extract_tasks_simple():
    results = local_nlp_extract_tasks("Submit homework by tomorrow")
    assert len(results) >= 1
    assert results[0]["title"]

def test_local_nlp_extract_tasks_reminder():
    results = local_nlp_extract_tasks("Remind me to call Mom")
    assert any(r["item_type"] == "reminder" for r in results)

def test_local_nlp_extract_tasks_empty():
    results = local_nlp_extract_tasks("")
    assert isinstance(results, list)


# ─── evidence_window ─────────────────────────────────────────────────────────

def test_evidence_window_finds_term():
    text = "A" * 200 + " submit homework " + "B" * 200
    window = evidence_window(text, {"title": "Submit Homework"})
    assert "submit homework" in window.lower()

def test_evidence_window_empty_source():
    assert evidence_window("", {"title": "Task"}) == ""


# ─── compact_text_for_extraction ─────────────────────────────────────────────

def test_compact_text_keeps_task_lines():
    # compact_text only filters lines when the full text exceeds MAX_AI_INPUT_CHARS.
    # With large input, noise lines (page numbers) are stripped while task lines stay.
    filler = "Page 1\n" * 3000  # ~21000 chars — triggers compaction
    text = filler + "Submit the report by Friday\n"
    result = compact_text_for_extraction(text)
    assert "Submit the report" in result

def test_compact_text_large_input():
    big = ("Submit assignment\n" * 2000)
    result = compact_text_for_extraction(big)
    assert len(result) <= 18100  # MAX_AI_INPUT_CHARS default + some slack


# ─── parse_current_time ──────────────────────────────────────────────────────

def test_parse_current_time_iso():
    dt = parse_current_time("2026-06-10T09:00:00Z")
    assert dt.year == 2026 and dt.month == 6 and dt.day == 10

def test_parse_current_time_with_annotation():
    dt = parse_current_time("2026-06-10T09:00:00 (Local Time)")
    assert dt.year == 2026

def test_parse_current_time_none_returns_now():
    dt = parse_current_time(None)
    assert isinstance(dt, datetime)
