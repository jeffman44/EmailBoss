"""Usage meter budget + the ported output guards."""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from tests.fakes_ai import FakeStore  # noqa: E402
from plannerboard.ai import guards  # noqa: E402
from plannerboard.ai.base import LLMResult  # noqa: E402
from plannerboard.ai.usage import UsageMeter, estimate_cost, lookup_price  # noqa: E402


# -- usage -------------------------------------------------------------------

def test_price_table_prefix_match_and_unknown_model():
    cost, est = estimate_cost("claude-sonnet-4-5-20250929", 1_000_000, 1_000_000)
    assert est and abs(cost - 18.0) < 1e-6
    cost, est = estimate_cost("some-mystery-model", 5000, 5000)
    assert cost == 0 and est is False
    assert lookup_price("ollama/qwen3.5:4b") == (0.0, 0.0)
    assert lookup_price("gpt-4o-mini-2024-07-18") == (0.15, 0.60)


def test_meter_budget_gate_and_ledger():
    store = FakeStore()
    meter = UsageMeter(store, daily_budget_usd=0.01)
    assert meter.allow() and not meter.budget_hit
    # 300k output tokens on sonnet is $4.50: way over a one-cent budget.
    res = LLMResult(text="x", input_tokens=10, output_tokens=300_000, model="claude-sonnet-4-5", ok=True)
    meter.record(res, task="triage.classify", provider="anthropic")
    assert res.cost_usd > 0.01 and res.cost_estimated
    assert store.usage[0]["task"] == "triage.classify" and store.usage[0]["provider"] == "anthropic"
    assert meter.budget_hit is True
    assert meter.allow() is False
    assert meter.today()["calls"] == 1 and meter.today()["by_task"] == {"triage.classify": 1}


def test_meter_zero_budget_means_uncapped():
    store = FakeStore()
    store._usage_today_override = {"cost_usd": 999.0, "calls": 9, "by_task": {}}
    meter = UsageMeter(store, daily_budget_usd=0)
    assert meter.allow() and not meter.budget_hit


# -- guards ------------------------------------------------------------------

def test_parse_json_tolerates_fences_and_prose():
    assert guards.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert guards.parse_json('Sure, here: {"a": {"b": [1, 2]}} hope that helps') == {"a": {"b": [1, 2]}}
    assert guards.parse_json('[1, 2, 3]') == [1, 2, 3]
    assert guards.parse_json('{"s": "brace } inside"}') == {"s": "brace } inside"}
    assert guards.parse_json("no json here") is None
    assert guards.parse_json("") is None


def test_grounding_drops_fabricated_number_and_keeps_grounded():
    source = "Received work order INV10107046 for the north lobby unit. Please quote 4 air curtains."
    bullets = [
        "• Work order INV10107046 received for the north lobby unit",   # grounded
        "• Received work order INV1",                                    # truncated identifier
        "• Quote requested for 4 air curtains at $12,450",               # invented amount
        "• Vendor ACME will deliver via FedEx on Tuesday",               # invented acronym + CamelCase
    ]
    kept = guards.ground_bullets(bullets, source)
    assert kept == ["• Work order INV10107046 received for the north lobby unit"]


def test_grounding_allowlist_from_profession_acronyms():
    source = "Can you send the updated pricing by Friday"
    bullets = ["• RTU pricing requested by Friday"]
    assert guards.ground_bullets(bullets, source) == []
    assert guards.ground_bullets(bullets, source, acronyms=["RTU"]) == bullets


def test_ground_text_sentence_level():
    source = "Thanks, we received your proposal and will review it this week."
    draft = "Thanks for confirming. We will have pricing to you by 3/14. I will follow up after your review."
    out = guards.ground_text(draft, source)
    assert "3/14" not in out and "follow up" in out


def test_meta_gloss_contact_filters():
    src = "Please call Kelly at 303-555-1212 to schedule access to the roof."
    bullets = [
        "• Acknowledgment of the sender's intent with a positive tone",
        "• Call Kelly at 303-555-1212 to schedule access",
        "• Contact is Kelly, office number 303-555-1212",
        "• Access to the roof (Rooftop Terminal Unit) requested",
    ]
    out = guards.filter_bullets(bullets, src)
    assert out == ["• Call Kelly at 303-555-1212 to schedule access", "• Access to the roof requested"]


def test_clean_body_uses_profile_signature_and_quote_markers():
    profile = {"user_name": "Pat Example", "role": "Account Manager", "company": "Example Co"}
    text = ("Can we move the walk to Thursday?\n\nPat Example\nAccount Manager\nExample Co\n"
            "Cell: 555-111-2222\n\nFrom: Sam <sam@client.test> Sent: Monday\nold stuff")
    assert guards.clean_body(text, profile=profile) == "Can we move the walk to Thursday?"
    assert guards.clean_body("Thanks, see below.\n> quoted line", profile=profile) == "Thanks, see below."


def test_verbatim_mode_thresholds():
    assert guards.verbatim_mode("ok thx") == ""                       # thin: no model, no card text
    short = "Can you send the certificate of insurance for the lobby job before Friday please"
    assert guards.verbatim_mode(short).startswith("• Can you send")  # verbatim, no model
    long = ("We walked the site today and found three items that need attention before the inspection. " * 4)
    assert guards.verbatim_mode(long) is None                         # model path


def test_strip_signature_from_draft():
    profile = {"user_name": "Pat Example", "role": "Account Manager", "company": "Example Co"}
    draft = "Hi Sam,\nGot it, the revised drawings are in hand.\nBest regards,\nPat Example\nAccount Manager"
    assert guards.strip_signature_from_draft(draft, profile) == "Got it, the revised drawings are in hand."
