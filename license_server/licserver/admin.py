"""Admin pages: accounts, issue license, renew, revoke, activations, audit log.

Gate: require_admin (session account with is_admin = 1).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import mailer
from .auth import create_account
from .licenses import PLANS, issue_license, license_state, renew_license, revoke_license
from .web import check_csrf, db_of, redirect, render, require_admin, settings_of

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_home(request: Request, admin: dict = Depends(require_admin)):
    db = db_of(request)
    accounts = db.list_accounts()
    licenses = db.list_licenses()
    for lic in licenses:
        lic["state"] = license_state(lic)
        lic["seats_used"] = len(db.active_activations(lic["lid"]))
    return render(request, "admin/index.html", accounts=accounts, licenses=licenses, plans=PLANS)


@router.get("/issue", response_class=HTMLResponse)
def issue_form(request: Request, admin: dict = Depends(require_admin)):
    return render(request, "admin/issue.html", plans=PLANS, email=request.query_params.get("email", ""))


@router.post("/issue")
def issue(request: Request, admin: dict = Depends(require_admin), email: str = Form(...), plan: str = Form("solo"),
          seats: int = Form(0), days: int = Form(0), features: str = Form(""), send_email: str = Form(""),
          csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    db, settings = db_of(request), settings_of(request)
    if plan not in PLANS:
        return render(request, "admin/issue.html", status_code=400, plans=PLANS, email=email, error="Unknown plan.")
    acct = db.get_account_by_email(email)
    created_note = ""
    if not acct:
        acct, temp = create_account(request, email, actor=admin["id"])
        mailer.send_welcome(acct["email"], settings.product_name, settings.base_url, temp_password=temp)
        created_note = " Account created and welcome mail queued."
    feats = [f.strip() for f in features.split(",") if f.strip()] or None
    lic = issue_license(request.app, acct, plan, seats=seats or None, days=days or None, features=feats,
                        actor=admin["id"])
    if send_email:
        mailer.send_license_key(acct["email"], settings.product_name, plan, lic["key"], settings.base_url)
    return redirect("/admin", notice=f"Issued {plan} license {lic['lid']} to {acct['email']}.{created_note}")


@router.post("/licenses/{lid}/revoke")
def revoke(request: Request, lid: str, admin: dict = Depends(require_admin), reason: str = Form(""),
           csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    lic = revoke_license(request.app, lid, actor=admin["id"], reason=reason)
    if not lic:
        return redirect("/admin", error="No such license.")
    return redirect("/admin", notice=f"Revoked {lid}.")


@router.post("/licenses/{lid}/renew")
def renew(request: Request, lid: str, admin: dict = Depends(require_admin), days: int = Form(365),
          csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    try:
        lic = renew_license(request.app, lid, days, actor=admin["id"])
    except KeyError:
        return redirect("/admin", error="No such license.")
    return redirect("/admin", notice=f"Renewed {lid} to {lic['expires']}. Key re-signed.")


@router.post("/accounts/{aid}/admin")
def toggle_admin(request: Request, aid: str, admin: dict = Depends(require_admin), value: str = Form("1"),
                 csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    db = db_of(request)
    if aid == admin["id"] and value != "1":
        return redirect("/admin", error="You cannot remove your own admin flag.")
    db.set_admin(aid, value == "1")
    db.audit("account.admin", actor=admin["id"], target=aid, detail={"is_admin": value == "1"})
    return redirect("/admin", notice="Admin flag updated.")


@router.get("/activations", response_class=HTMLResponse)
def activations(request: Request, admin: dict = Depends(require_admin)):
    db = db_of(request)
    return render(request, "admin/activations.html", activations=db.all_activations(limit=500))


@router.post("/activations/{lid}/{act_id}/revoke")
def revoke_activation(request: Request, lid: str, act_id: int, admin: dict = Depends(require_admin),
                      csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    db = db_of(request)
    act = db.get_activation_by_id(act_id)
    if act and act["lid"] == lid:
        db.revoke_activation(lid, act["fingerprint"])
        db.audit("admin.deactivate_device", actor=admin["id"], target=lid, detail={"fingerprint": act["fingerprint"][:12]})
    return redirect("/admin/activations", notice="Seat released.")


@router.get("/audit", response_class=HTMLResponse)
def audit(request: Request, admin: dict = Depends(require_admin)):
    return render(request, "admin/audit.html", entries=db_of(request).audit_entries(limit=300))
