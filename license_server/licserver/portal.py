"""Customer portal pages: dashboard (my licenses + devices), download, home."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .ai_relay import AiError, _packs, create_checkout_session
from .licenses import license_state
from .web import check_csrf, current_account, db_of, redirect, render, require_account, settings_of

router = APIRouter()


def _decorate(db, lic: dict) -> dict:
    lic = dict(lic)
    lic["state"] = license_state(lic)
    lic["devices"] = db.all_activations(lic["lid"])
    lic["seats_used"] = sum(1 for d in lic["devices"] if not d["revoked"])
    wallet = db.get_wallet(lic["lid"])
    lic["ai_wallet"] = wallet
    lic["ai_totals"] = db.ledger_totals(lic["lid"], days=30)
    return lic


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    if current_account(request):
        return redirect("/dashboard")
    return render(request, "home.html")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, account: dict = Depends(require_account)):
    db = db_of(request)
    licenses = [_decorate(db, lic) for lic in db.licenses_for_account(account["id"])]
    return render(request, "dashboard.html", licenses=licenses, ai_packs=_packs(settings_of(request)),
                  ai_trial=settings_of(request).ai_trial_credits)


@router.post("/ai/checkout")
def ai_buy(request: Request, account: dict = Depends(require_account), lid: str = Form(""), pack: str = Form(""),
           csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    lic = db_of(request).get_license(lid)
    if not lic or lic["account_id"] != account["id"]:
        return redirect("/dashboard", error="That license is not yours.")
    try:
        out = create_checkout_session(request.app, lid, pack)
    except AiError as e:
        return redirect("/dashboard", error=e.message)
    return redirect(out["url"])


@router.post("/licenses/{lid}/devices/{act_id}/deactivate")
def deactivate_device(request: Request, lid: str, act_id: int, account: dict = Depends(require_account),
                      csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    db = db_of(request)
    lic = db.get_license(lid)
    if not lic or (lic["account_id"] != account["id"] and not account.get("is_admin")):
        return redirect("/dashboard", error="That license is not yours.")
    act = db.get_activation_by_id(act_id)
    if not act or act["lid"] != lid:
        return redirect("/dashboard", error="Device not found.")
    db.revoke_activation(lid, act["fingerprint"])
    db.audit("portal.deactivate_device", actor=account["id"], target=lid, detail={"fingerprint": act["fingerprint"][:12]})
    return redirect("/dashboard", notice="Device released. The app on it will drop to read-only at its next check-in.")


@router.get("/download", response_class=HTMLResponse)
def download(request: Request, account: dict = Depends(require_account)):
    s = settings_of(request)
    labels = {"windows": "Windows (installer)", "macos": "macOS (dmg)", "linux": "Linux (tar.gz)"}
    links = [{"id": k, "label": labels.get(k, k.replace("_", " ").title()), "url": v} for k, v in sorted(s.download_urls.items())]
    return render(request, "download.html", links=links)


@router.get("/healthz")
def healthz(request: Request):
    db = db_of(request)
    try:
        n = db.account_count()
        return {"ok": True, "accounts": n, "key_id": request.app.state.signer.key_id()}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": str(exc)}
