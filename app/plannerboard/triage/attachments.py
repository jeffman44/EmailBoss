"""Attachment text extraction (pdf / xlsx / csv) with a per-file timeout.

Ported from engine/email_processor.py lines 728-1086 (document-type by
filename, pdfplumber / openpyxl / csv extractors, labeled amounts, the
15-second per-file guard) and 1133-1339 (``extract_attachment_data``: the
case-exact per-message directory, only DECLARED files, the loose-file
fallback, the inline-image gap check). Optional dependencies are guarded:
without pdfplumber / openpyxl the entry carries an ``error`` note and the
email still flows.

Trade vocabulary (equipment regexes) is gone; what remains is generic:
amounts, labeled totals, profession id patterns, model / part numbers,
addresses, schedule phrases, contacts, tables.

The timeout uses ``signal.alarm`` where available (POSIX main thread) and a
worker thread with ``join(timeout)`` elsewhere, so a hung extraction can
never stall the triage cycle.
"""
from __future__ import annotations

import csv
import re
import signal
import threading
from pathlib import Path

from .match import ORDER_REF_RE, TRACKING_RE, TRACKING_CTX_RE, extract_ids, sender_domain

PER_FILE_TIMEOUT = 15
INLINE_PHOTO_MIN_BYTES = 60000

DOLLAR_RE = re.compile(
    r'(?:total|grand total|quoted at|price|pricing|proposal total|project total'
    r'|approved for|per your quote of|sum of|contract (?:price|amount|sum)'
    r'|not to exceed|lump sum|for the sum|amount due|balance due|fee)\s*[:=]?\s*\$?([\d,]+\.?\d{0,2})',
    re.IGNORECASE)
DOLLAR_ANY_RE = re.compile(r'\$\s*([\d,]+\.?\d{0,2})')
MODEL_RE = re.compile(
    r'(?:model|mod(?:el)?\.?|part|p/n|part\s*#?|catalog|cat\.?\s*#?|sku|item\s*#)\s*#?\s*:?\s*([A-Z0-9][\w-]{4,30})',
    re.IGNORECASE)
ADDRESS_RE = re.compile(
    r'\b\d{1,6}\s+(?:[A-Z][a-zA-Z]*\.?\s){1,4}'
    r'(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Pkwy|Parkway|Ct|Court|Pl|Place|Cir|Circle)\b\.?',
    re.IGNORECASE)
SCHEDULE_RE = re.compile(
    r'(?:lead\s*time|ship(?:ping)?\s*date|delivery\s*date|eta|due\s*date|deadline|start\s*date|'
    r'completion\s*date|closing\s*date)\s*[:=]?\s*[^\n]{0,40}',
    re.IGNORECASE)
SCOPE_PHRASES = ["scope of work", "scope:", "description of work", "work to be performed",
                 "we propose", "this proposal includes", "deliverables", "summary of services",
                 "statement of work", "includes the following"]


def classify_attachment(filename: str, content_type: str = "") -> str:
    """Document type by filename. Generic labels: proposal / contract /
    invoice / plans / spec / other, per extension family."""
    n = (filename or "").lower()
    if n.endswith(".pdf"):
        if any(kw in n for kw in ("plan", "drawing", "arch", "mech", "elec", "plum", "stamp", "cd_", "sheet")):
            return "pdf_plans"
        if any(kw in n for kw in ("proposal", "quote", "pricing", "estimate", "bid", "offer")):
            return "pdf_proposal"
        if any(kw in n for kw in ("contract", "agreement", "signed", "executed", "lease", "engagement")):
            return "pdf_contract"
        if any(kw in n for kw in ("spec", "submittal", "cut sheet", "cutsheet", "datasheet", "iom")):
            return "pdf_spec"
        if any(kw in n for kw in ("invoice", "receipt", "statement", "po_", "po-", "purchase")):
            return "pdf_invoice"
        if any(kw in n for kw in ("report", "summary", "findings", "assessment")):
            return "pdf_report"
        return "pdf_other"
    if n.endswith((".xlsx", ".xls", ".xlsm")):
        return "excel"
    if n.endswith((".csv", ".tsv")):
        return "csv"
    if n.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".heic", ".webp")):
        return "image"
    if n.endswith((".docx", ".doc")):
        return "word"
    return "other"


def _amounts(text: str) -> tuple[list[float], list[dict]]:
    vals = set()
    for m in DOLLAR_ANY_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
            if v >= 50:
                vals.add(v)
        except ValueError:
            pass
    labeled = []
    for m in DOLLAR_RE.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if v > 0:
            start = max(0, m.start() - 20)
            labeled.append({"amount": v, "context": text[start:m.end()].strip()[:80]})
    return sorted(vals, reverse=True)[:20], labeled[:10]


def _common_fields(full_text: str, result: dict, id_patterns) -> None:
    result["dollar_amounts"], labeled = _amounts(full_text)
    if labeled:
        result["labeled_amounts"] = labeled
    ids = sorted({val for _k, val, _u in extract_ids(full_text, id_patterns or [])})
    if ids:
        result["id_refs"] = ids[:10]
    ord_raw = set()
    for m in ORDER_REF_RE.finditer(full_text):
        v = m.group(1).strip().upper().rstrip("-")
        if len(v) >= 5 and re.search(r"\d", v):
            ord_raw.add(v)
    if ord_raw:
        result["order_numbers"] = sorted(ord_raw)[:10]
    trk = sorted({m.group(1).upper() for m in TRACKING_RE.finditer(full_text)}
                 | {m.group(1).upper() for m in TRACKING_CTX_RE.finditer(full_text)})
    if trk:
        result["tracking_numbers"] = trk[:10]
    result["model_numbers"] = sorted({m.group(1).strip() for m in MODEL_RE.finditer(full_text)})[:15]
    result["addresses"] = sorted({m.group(0).strip() for m in ADDRESS_RE.finditer(full_text)})[:5]
    result["schedule_refs"] = [m.group(0).strip()[:60] for m in SCHEDULE_RE.finditer(full_text)][:5]
    low = full_text.lower()
    snippets = []
    for phrase in SCOPE_PHRASES:
        idx = low.find(phrase)
        if idx >= 0:
            snip = full_text[idx:idx + 300].strip()
            for end_char in (". ", ".\n", "\n\n"):
                cut = snip.find(end_char, len(phrase))
                if cut > 0:
                    snip = snip[:cut + 1]
                    break
            snippets.append(snip[:200])
    result["scope_snippets"] = snippets[:5]
    result["contacts"] = {
        "emails": sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", full_text)))[:10],
        "phones": sorted(set(re.findall(r"(?:\+?1[-.]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", full_text)))[:10],
    }


def extract_pdf_data(filepath, id_patterns=None) -> dict:
    try:
        import pdfplumber
    except ImportError:
        return {"error": "pdfplumber not installed", "text_summary": ""}
    result = {"text_summary": "", "full_text_length": 0, "page_count": 0, "tables": []}
    try:
        with pdfplumber.open(str(filepath)) as pdf:
            result["page_count"] = len(pdf.pages)
            texts = []
            for i, page in enumerate(pdf.pages):
                texts.append(page.extract_text() or "")
                if i < 5:
                    try:
                        for table in (page.extract_tables() or []):
                            if table and len(table) > 1:
                                result["tables"].append(
                                    [[str(c).strip() if c else "" for c in row] for row in table[:20]])
                    except Exception:
                        pass
            full = "\n".join(texts)
            result["full_text_length"] = len(full)
            result["text_summary"] = full[:2000].strip()
            _common_fields(full, result, id_patterns)
    except Exception as e:
        result["error"] = f"PDF extraction failed: {str(e)[:200]}"
    return result


def extract_pdf_plans(filepath, id_patterns=None) -> dict:
    try:
        import pdfplumber
    except ImportError:
        return {"error": "pdfplumber not installed", "text_summary": ""}
    try:
        with pdfplumber.open(str(filepath)) as pdf:
            first = (pdf.pages[0].extract_text() or "")[:500] if pdf.pages else ""
            return {"page_count": len(pdf.pages), "text_summary": first,
                    "addresses": [m.group(0).strip() for m in ADDRESS_RE.finditer(first)],
                    "extraction_note": "Plans/drawings: minimal text extraction only"}
    except Exception as e:
        return {"error": f"PDF extraction failed: {str(e)[:200]}", "text_summary": ""}


def extract_excel_data(filepath, id_patterns=None) -> dict:
    try:
        import openpyxl
    except ImportError:
        return {"error": "openpyxl not installed"}
    result = {"sheet_names": [], "row_count": 0, "dollar_amounts": [], "headers": [],
              "summary_data": [], "key_cells": []}
    try:
        wb = openpyxl.load_workbook(str(filepath), data_only=True, read_only=True)
        result["sheet_names"] = wb.sheetnames
        all_text = []
        for sheet_name in wb.sheetnames[:5]:
            ws = wb[sheet_name]
            rows_text = []
            row_count = 0
            for row in ws.iter_rows(max_row=200, values_only=True):
                row_count += 1
                row_strs = [str(c) if c is not None else "" for c in row]
                rows_text.append(" | ".join(row_strs))
                if row_count <= 3:
                    non_empty = [s for s in row_strs if s.strip()]
                    if non_empty:
                        result["headers"].append({"sheet": sheet_name, "row": row_count, "cells": non_empty[:15]})
                for cell in row:
                    if cell is None:
                        continue
                    cs = str(cell).strip()
                    if isinstance(cell, (int, float)) and cell >= 50:
                        result["dollar_amounts"].append(float(cell))
                    if any(kw in cs.lower() for kw in ("total", "subtotal", "amount", "price", "cost", "fee")):
                        result["key_cells"].append({
                            "sheet": sheet_name, "row": row_count, "label": cs[:50],
                            "row_values": [str(c)[:30] for c in row if c is not None][:8]})
            result["row_count"] += row_count
            non_empty_rows = [r for r in rows_text if r.replace("|", "").strip()]
            if non_empty_rows:
                result["summary_data"].append({"sheet": sheet_name, "last_rows": non_empty_rows[-5:]})
            all_text.extend(rows_text)
        wb.close()
        result["dollar_amounts"] = sorted(set(result["dollar_amounts"]), reverse=True)[:20]
        full = "\n".join(all_text)
        ids = sorted({val for _k, val, _u in extract_ids(full, id_patterns or [])})
        if ids:
            result["id_refs"] = ids[:10]
        result["model_numbers"] = sorted({m.group(1).strip() for m in MODEL_RE.finditer(full)})[:15]
        result["text_summary"] = full[:2000]
    except Exception as e:
        result["error"] = f"Excel extraction failed: {str(e)[:200]}"
    return result


def extract_csv_data(filepath, id_patterns=None) -> dict:
    result = {"row_count": 0, "headers": [], "dollar_amounts": [], "sample_rows": []}
    try:
        filepath = Path(filepath)
        delimiter = "\t" if filepath.suffix.lower() == ".tsv" else ","
        amounts = set()
        with filepath.open("r", encoding="utf-8", errors="replace") as f:
            for i, row in enumerate(csv.reader(f, delimiter=delimiter)):
                if i == 0:
                    result["headers"] = row[:15]
                elif i <= 5:
                    result["sample_rows"].append(row[:15])
                result["row_count"] += 1
                for cell in row:
                    for m in DOLLAR_ANY_RE.finditer(str(cell)):
                        try:
                            v = float(m.group(1).replace(",", ""))
                            if v >= 50:
                                amounts.add(v)
                        except ValueError:
                            pass
                if i > 500:
                    break
        result["dollar_amounts"] = sorted(amounts, reverse=True)[:20]
    except Exception as e:
        result["error"] = f"CSV extraction failed: {str(e)[:200]}"
    return result


# ── per-file timeout ────────────────────────────────────────────────────────

class _TimeoutError(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _TimeoutError("attachment extraction timed out")


def extract_with_timeout(func, filepath, timeout_sec: int = PER_FILE_TIMEOUT, **kw) -> dict:
    """Run ``func(filepath, **kw)`` with a hard time budget. signal.alarm on a
    POSIX main thread; a daemon worker + join elsewhere (Windows, threads)."""
    use_signal = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if use_signal:
        old = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(int(timeout_sec))
        try:
            return func(filepath, **kw)
        except _TimeoutError:
            return {"error": f"Extraction timed out after {timeout_sec}s", "text_summary": ""}
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    box: dict = {}

    def _run():
        try:
            box["result"] = func(filepath, **kw)
        except Exception as e:
            box["result"] = {"error": f"Extraction failed: {str(e)[:200]}", "text_summary": ""}

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        return {"error": f"Extraction timed out after {timeout_sec}s", "text_summary": ""}
    return box.get("result") or {"error": "no result", "text_summary": ""}


# ── inline image gap check ──────────────────────────────────────────────────

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_IMG_CID_RE = re.compile(r"""src\s*=\s*["']cid:([^"']+)["']""", re.I)
_IMG_SIZE_RE = re.compile(r"""\bsize\s*=\s*["'](\d+)["']""", re.I)


def extract_inline_image_refs(html: str, min_bytes: int = INLINE_PHOTO_MIN_BYTES) -> list[dict]:
    refs = []
    for tag in _IMG_TAG_RE.findall(html or ""):
        cid = _IMG_CID_RE.search(tag)
        if not cid:
            continue
        size_m = _IMG_SIZE_RE.search(tag)
        size = int(size_m.group(1)) if size_m else -1
        if size == -1 or size >= min_bytes:
            refs.append({"cid": cid.group(1)[:120], "size": size})
    return refs


# ── the driver ──────────────────────────────────────────────────────────────

def attachment_files(mail_cache, email_obj: dict) -> list[Path]:
    """Files on disk for this message: the case-EXACT per-message directory,
    only files the record declared; else loose files matched by name."""
    attach_root = Path(mail_cache) / "attachments"
    msg_id = email_obj.get("message_id") or ""
    if not attach_root.is_dir() or not msg_id:
        return []
    msg_dir = None
    try:
        for d in attach_root.iterdir():
            if d.is_dir() and d.name == msg_id:
                msg_dir = d
                break
    except OSError:
        return []
    declared = None
    if isinstance(email_obj.get("attachments"), list):
        declared = {(a.get("name", "") if isinstance(a, dict) else str(a)) for a in email_obj["attachments"]}
        declared.discard("")
    files: list[Path] = []
    if msg_dir is not None:
        files = [f for f in sorted(msg_dir.iterdir())
                 if f.is_file() and (declared is None or f.name in declared)]
    if not files and declared:
        for name in declared:
            loose = attach_root / name
            if loose.is_file():
                files.append(loose)
    return files


def extract_attachment_data(mail_cache, email_obj: dict, id_patterns=None,
                            timeout_sec: int = PER_FILE_TIMEOUT) -> list[dict]:
    """Populate ``email_obj['attachment_data']`` (and the inline-image gap
    fields). Returns the attachment_data list. Never raises."""
    files = attachment_files(mail_cache, email_obj)

    # inline gap check: photo-sized cid: refs vs image files on disk
    try:
        refs = extract_inline_image_refs(email_obj.get("body_raw") or "")
        if refs:
            img_exts = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".bmp", ".webp"}
            on_disk = sum(1 for f in files if f.suffix.lower() in img_exts)
            inline_atts = [a for a in (email_obj.get("attachments") or [])
                           if isinstance(a, dict) and a.get("isInline")]
            expected = (sum(1 for a in inline_atts if int(a.get("size") or 0) >= INLINE_PHOTO_MIN_BYTES)
                        if inline_atts else len(refs))
            gap = expected - on_disk
            if gap > 0:
                email_obj["missing_inline_images"] = gap
                email_obj["inline_image_refs"] = refs
    except Exception:
        pass

    data = []
    for fpath in files:
        ftype = classify_attachment(fpath.name)
        if ftype == "image":
            continue
        try:
            size = fpath.stat().st_size
        except OSError:
            data.append({"filename": fpath.name, "file_type": ftype,
                         "error": "File inaccessible (cloud-only or sync pending)"})
            continue
        entry = {"filename": fpath.name, "file_type": ftype, "size_bytes": size}
        try:
            if ftype == "pdf_plans":
                entry.update(extract_with_timeout(extract_pdf_plans, fpath, timeout_sec, id_patterns=id_patterns))
            elif ftype.startswith("pdf"):
                entry.update(extract_with_timeout(extract_pdf_data, fpath, timeout_sec, id_patterns=id_patterns))
            elif ftype == "excel":
                entry.update(extract_with_timeout(extract_excel_data, fpath, timeout_sec, id_patterns=id_patterns))
            elif ftype == "csv":
                entry.update(extract_with_timeout(extract_csv_data, fpath, timeout_sec, id_patterns=id_patterns))
            else:
                entry["extraction_note"] = f"Skipped extraction for type: {ftype}"
        except Exception as e:
            entry["error"] = f"Extraction failed: {str(e)[:200]}"
        data.append(entry)

    if data:
        dom = sender_domain(email_obj.get("from_email") or "")
        for entry in data:
            refs = set(entry.get("vendor_refs") or [])
            for num in entry.get("order_numbers") or []:
                refs.add(f"ORD:{dom}:{num}" if dom else f"ORD:{num}")
            for num in entry.get("tracking_numbers") or []:
                refs.add(f"TRK:{num}")
            if refs:
                entry["vendor_refs"] = sorted(refs)
    email_obj["attachment_data"] = data
    return data


def labeled_total(attachment_data: list[dict]) -> float | None:
    """The largest labeled amount on a proposal/invoice-typed attachment."""
    best = None
    for att in attachment_data or []:
        if not str(att.get("file_type", "")).startswith("pdf"):
            continue
        for la in att.get("labeled_amounts") or []:
            v = la.get("amount")
            if isinstance(v, (int, float)) and (best is None or v > best):
                best = float(v)
    return best
