"""Email classification: rules fallback + optional LLM.

Ported from v2/ai.py ``_fallback_classify`` (lines 420-438) and the label
validation in ``classify_email`` (407-416), generalized to the profession's
``triage.classifications`` list. Output::

    {"classification": <label>, "needs_reply": bool, "follow_up_days": int,
     "auto_action": str, "confidence_note": str}

The LLM hook: when an ``llm`` object is given, ``llm.run("triage.classify",
vars)`` is called; its result is trusted only if it names a valid label.
Anything else (None, error, unknown label) falls back to the rules.
"""
from __future__ import annotations

import re

from .match import is_internal
from .text import strip_signature_block

# Generic label families. The profession list decides which are available;
# the first available label in each tuple is used, so an "hvac" template can
# say "quote_request" where "general" says "request".
LABEL_FAMILIES = {
    "request": ("request", "quote_request", "service_request", "inquiry"),
    "client_reply": ("client_reply", "customer_reply", "reply"),
    "approval": ("approval", "approval_received", "approved"),
    "vendor": ("vendor", "vendor_quote", "supplier"),
    "scheduling": ("scheduling", "schedule"),
    "internal": ("internal", "internal_reply", "internal_handoff"),
    "informational": ("informational", "fyi", "notice"),
    "needs_reply": ("needs_reply", "follow_up_needed"),
    "no_reply": ("no_reply", "general_reply"),
    "junk": ("junk", "spam", "notification"),
    "sent": ("proposal_sent", "quote_sent", "sent", "outbound"),
}

FAMILY_FALLBACK = {
    "sent": ("no_reply", "informational"),
    "junk": ("informational", "no_reply"),
    "vendor": ("informational", "client_reply"),
    "approval": ("client_reply", "needs_reply"),
    "scheduling": ("needs_reply", "client_reply"),
    "request": ("needs_reply", "client_reply"),
    "internal": ("informational", "client_reply"),
}

_REQUEST_RE = re.compile(r"\b(quote|pricing|price|estimate|proposal|how much|rfp|rfq|bid|can you (?:help|send|provide|look)|"
                         r"please (?:send|provide|advise|quote)|request(?:ing)?|need(?:s)? (?:a|an|your)|would like to)\b", re.I)
_APPROVAL_RE = re.compile(r"\b(approved|go ahead|please proceed|move forward|green light|signed|accept(?:ed)? (?:the|your))\b", re.I)
_VENDOR_RE = re.compile(r"\b(order|shipment|shipped|delivered|tracking|invoice|backorder|lead time|availability|packing slip|"
                        r"quote attached|our quote|pricing attached)\b", re.I)
_SCHED_RE = re.compile(r"\b(schedule|scheduling|reschedule|availability|what time|calendar|appointment|meeting|"
                       r"when can|are you available|book(?:ing)?)\b", re.I)
_INFO_RE = re.compile(r"\b(fyi|for your (?:records|information)|no action (?:needed|required)|just (?:letting you know|a heads up)|"
                      r"heads up|newsletter|unsubscribe|automatic reply|out of office)\b", re.I)
_QUESTION_RE = re.compile(r"\?")
_ROBOT_LOCAL_RE = re.compile(r"^(no.?reply|do.?not.?reply|auto.?reply|notifications?|mailer|daemon|postmaster|bounce|alerts?)",
                             re.I)


def available_labels(profession: dict | None) -> list[str]:
    labels = ((profession or {}).get("triage") or {}).get("classifications") or []
    return [str(x) for x in labels] or ["request", "client_reply", "approval", "vendor", "scheduling",
                                         "internal", "informational", "needs_reply", "no_reply", "junk"]


def pick_label(family: str, labels: list[str]) -> str:
    for cand in LABEL_FAMILIES.get(family, (family,)):
        if cand in labels:
            return cand
    if family in labels:
        return family
    # The profession lacks this family: fall back to the nearest one it has.
    for fam in FAMILY_FALLBACK.get(family, ()) + ("client_reply", "needs_reply", "informational", "no_reply"):
        for cand in LABEL_FAMILIES.get(fam, (fam,)):
            if cand in labels:
                return cand
    return labels[0] if labels else family


def is_robot_sender(from_email: str) -> bool:
    local = (from_email or "").split("@")[0].lower()
    return bool(_ROBOT_LOCAL_RE.match(local))


def rules_classify(email_obj: dict, profession: dict | None, cfg: dict | None) -> dict:
    labels = available_labels(profession)
    direction = email_obj.get("direction", "inbound")
    from_email = (email_obj.get("from_email") or "").lower()
    internal_domains = ((cfg or {}).get("user") or {}).get("internal_domains") or []
    subject = email_obj.get("subject") or ""
    body = strip_signature_block(email_obj.get("body") or email_obj.get("body_preview") or "")
    text = f"{subject}\n{body}"[:1500]
    signals = email_obj.get("triage_signals") or {}

    def out(family, needs_reply, days, action, note="rules"):
        return {"classification": pick_label(family, labels), "needs_reply": bool(needs_reply),
                "follow_up_days": int(days), "auto_action": action, "confidence_note": note}

    if direction == "outbound":
        if re.search(r"\b(proposal|quote|estimate|pricing|offer)\b", text, re.I) and (
                email_obj.get("has_attachments") or any(
                    str(a.get("file_type", "")).startswith("pdf_proposal")
                    for a in email_obj.get("attachment_data") or [])):
            return out("sent", False, 5, "Follow up if no response")
        if _REQUEST_RE.search(text) and _QUESTION_RE.search(body):
            return out("no_reply", False, 3, "Waiting on their answer")
        return out("no_reply", False, 3, "Check for a response")

    if is_robot_sender(from_email) or _INFO_RE.search(subject):
        return out("informational", False, 0, "No reply needed")
    if is_internal(from_email, internal_domains):
        return out("internal", _QUESTION_RE.search(body) is not None, 1, "Review and respond internally")
    if signals.get("approval") or _APPROVAL_RE.search(text):
        return out("approval", True, 1, "Confirm the go-ahead and start the next step")
    if _REQUEST_RE.search(text):
        return out("request", True, 2, "Respond with the requested information or a proposal")
    if _SCHED_RE.search(text):
        return out("scheduling", True, 1, "Confirm the date and time")
    if _VENDOR_RE.search(text):
        return out("vendor", bool(_QUESTION_RE.search(body)), 2, "File the vendor update")
    if _QUESTION_RE.search(body):
        return out("needs_reply", True, 2, "Answer their question")
    return out("client_reply", True, 3, "Review and respond")


def _llm_vars(email_obj: dict, profession: dict | None, cfg: dict | None, project: dict | None) -> dict:
    user = (cfg or {}).get("user") or {}
    return {
        "direction": email_obj.get("direction", "inbound"),
        "from_name": email_obj.get("from_name") or email_obj.get("from_email") or "unknown",
        "from_email": email_obj.get("from_email") or "",
        "subject": email_obj.get("subject") or "(no subject)",
        "body": strip_signature_block(email_obj.get("body") or email_obj.get("body_preview") or "")[:1200],
        "classifications": ", ".join(available_labels(profession)),
        "project_name": (project or {}).get("name") or "",
        "project_status": (project or {}).get("status") or "",
        "user_name": user.get("name") or "",
        "company": user.get("company") or "",
        "industry": ((profession or {}).get("prompt_profile") or {}).get("industry") or "general business",
    }


def classify(email_obj: dict, profession: dict | None, cfg: dict | None,
             llm=None, project: dict | None = None) -> dict:
    """Rules result, overridden by the LLM only when it returns a valid label."""
    base = rules_classify(email_obj, profession, cfg)
    if llm is None:
        return base
    labels = available_labels(profession)
    try:
        res = llm.run("triage.classify", _llm_vars(email_obj, profession, cfg, project))
    except Exception as e:
        base["confidence_note"] = f"rules (llm error: {type(e).__name__})"
        return base
    parsed = None
    if isinstance(res, dict):
        parsed = res.get("parsed") if "parsed" in res else res
    else:
        parsed = getattr(res, "parsed", None)
    if not isinstance(parsed, dict):
        return base
    label = str(parsed.get("classification") or "").strip()
    if label not in labels:
        return base
    days = parsed.get("follow_up_days", base["follow_up_days"])
    try:
        days = max(0, min(10, int(days)))
    except (TypeError, ValueError):
        days = base["follow_up_days"]
    needs = parsed.get("needs_reply")
    if not isinstance(needs, bool):
        needs = base["needs_reply"]
    return {"classification": label, "needs_reply": needs, "follow_up_days": days,
            "auto_action": str(parsed.get("auto_action") or base["auto_action"])[:300],
            "confidence_note": "llm"}
