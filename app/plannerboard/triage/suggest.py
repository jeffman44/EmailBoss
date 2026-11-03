"""Deterministic new-project suggestion for an unmatched email.

Ported from engine/project_suggest.py: ``clean_subject`` (lines 306-323),
``guess_site_from_subject`` (326-364, the longest capitalized run), and the
``suggest_project`` shape (510-556). The trade vocabulary is replaced by the
profession's ``vocab.keywords`` / ``vocab.scope_phrases``; the client comes
from the sender's display name or domain when the subject does not name one.

Output::

    {"client": str, "client_source": "contact|domain|subject_heuristic|",
     "topic": str, "name": str, "confidence": "high|medium|low|",
     "ids": [...], "refs": [...], "is_new_client": bool}
"""
from __future__ import annotations

import re

from .match import compile_id_patterns, extract_ids, extract_vendor_refs, sender_domain, is_internal

REPLY_PREFIX_RE = re.compile(r"^\s*(?:re|fw|fwd|aw|tr|rv|sv)\s*(?:\[\d+\])?\s*:\s*", re.IGNORECASE)
DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b")
NOISE_RE = re.compile(r"[|/\\:;,()\[\]{}\"]+| - | -- ")
NOT_A_NAME = {"the", "a", "an", "of", "for", "and", "at", "in", "on", "to", "re", "fw", "fwd",
              "new", "update", "updated", "urgent", "question", "request", "quote", "proposal",
              "invoice", "follow", "up", "meeting", "call", "status", "info", "information",
              "monday", "tuesday", "wednesday", "thursday", "friday", "today", "tomorrow"}
_EQUIP_TOKEN_RE = re.compile(r"^[A-Za-z]{1,4}-?\d+[A-Za-z]?$")
_FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "aol.com",
              "live.com", "msn.com", "me.com", "comcast.net", "att.net", "proton.me", "protonmail.com"}


def clean_subject(subject: str, id_patterns=None) -> str:
    s = str(subject or "")
    prev = None
    while prev != s:
        prev = s
        s = REPLY_PREFIX_RE.sub("", s)
    for p in id_patterns or []:
        s = p["re"].sub(" ", s)
    s = re.sub(r"(?:order|invoice|ref(?:erence)?|ticket|case|quote)\s*(?:number|no\.?|num)?\s*[#:]+\s*[\w-]+", " ", s, flags=re.I)
    s = DATE_RE.sub(" ", s)
    s = re.sub(r"#\s*:?\s*[\w-]+", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -|,;:")


def guess_name_from_subject(subject: str, vocab_words: set[str], id_patterns=None) -> str:
    """Longest run of capitalized words in the cleaned subject that are not
    profession vocabulary: usually the client or place the email is about."""
    cleaned = NOISE_RE.sub("|", clean_subject(subject, id_patterns))
    best: list[str] = []
    for chunk in cleaned.split("|"):
        run: list[str] = []
        for tok in chunk.split():
            bare = tok.strip(".,;:!?()[]{}\"'")
            if not bare:
                continue
            titled = bare[0].isupper() or bare[0].isdigit()
            is_vocab = bare.lower() in vocab_words or bare.lower() in NOT_A_NAME
            if titled and (not is_vocab or (run and bare.lower() in NOT_A_NAME)):
                run.append(bare)
                continue
            if titled and bare.lower() in NOT_A_NAME and not run:
                run.append(bare)
                continue
            if len(run) > len(best):
                best = run
            run = []
        if len(run) > len(best):
            best = run
    while best and (best[-1].lower() in NOT_A_NAME or _EQUIP_TOKEN_RE.match(best[-1])):
        best.pop()
    while best and best[0].lower() in NOT_A_NAME and best[0].lower() != "the":
        best.pop(0)
    if not best or all(b.lower() in NOT_A_NAME for b in best):
        return ""
    return " ".join(best)


def _vocab(profession: dict | None) -> tuple[list[str], set[str]]:
    v = (profession or {}).get("vocab") or {}
    keywords = [str(k) for k in (v.get("keywords") or []) if k]
    acronyms = [str(a) for a in (v.get("acronyms") or []) if a]
    words = set()
    for k in keywords + acronyms + [str(s) for s in (v.get("scope_phrases") or [])]:
        for w in k.lower().split():
            words.add(w)
    return keywords + acronyms, words


def extract_topic(subject: str, body: str, profession: dict | None) -> str:
    """The longest profession keyword present in the subject (then body)."""
    keywords, _w = _vocab(profession)
    subj = " " + (subject or "").lower() + " "
    text = subj + " " + (body or "").lower()[:600] + " "
    best, best_len = "", 0
    for hay in (subj, text):
        for kw in keywords:
            k = kw.lower()
            if len(k) > best_len and k in hay:
                best, best_len = kw, len(k)
        if best:
            break
    return best.title() if best and best.islower() else best


def client_from_sender(email_obj: dict, cfg: dict | None, contact_index: dict | None = None) -> tuple[str, str]:
    """(client, source) from the sender: a known contact's project is handled
    by the matcher, so here it is the display name or the company domain."""
    from_email = (email_obj.get("from_email") or "").lower()
    internal = ((cfg or {}).get("user") or {}).get("internal_domains") or []
    if not from_email or is_internal(from_email, internal):
        return "", ""
    dom = sender_domain(from_email)
    if dom and dom not in _FREE_MAIL:
        base = dom.split(".")[0]
        if len(base) >= 3:
            return base.capitalize(), "domain"
    name = (email_obj.get("from_name") or "").strip()
    if name and "@" not in name:
        return name, "contact"
    return "", ""


def suggest_project(email_obj: dict, profession: dict | None, cfg: dict | None,
                    known_clients=()) -> dict:
    subject = email_obj.get("subject") or ""
    body = email_obj.get("body") or ""
    patterns = compile_id_patterns(profession)
    _kw, vocab_words = _vocab(profession)
    client, source = "", ""
    guess = guess_name_from_subject(subject, vocab_words, patterns)
    if guess and len(guess) >= 3:
        client, source = guess, "subject_heuristic"
    if not client:
        client, source = client_from_sender(email_obj, cfg)
    topic = extract_topic(subject, body, profession)
    ids = [val for _k, val, _u in extract_ids(subject + " " + body[:800], patterns)]
    refs = extract_vendor_refs(subject + " " + body[:2000], sender_domain(email_obj.get("from_email") or ""))
    if client and topic:
        name = f"{client} {topic}"
    elif client:
        name = client
    elif topic:
        name = topic
    else:
        name = clean_subject(subject, patterns)[:80]
    if client and topic and source != "subject_heuristic":
        confidence = "high"
    elif client and topic:
        confidence = "medium"
    elif client or topic:
        confidence = "low"
    else:
        confidence = ""
    known = {str(k).lower() for k in known_clients or ()}
    is_new = bool(client) and client.lower() not in known and not any(
        set(client.lower().split()) & set(k.split()) for k in known)
    return {"client": client, "client_source": source, "topic": topic, "name": name,
            "confidence": confidence, "ids": ids, "refs": refs, "is_new_client": is_new}
