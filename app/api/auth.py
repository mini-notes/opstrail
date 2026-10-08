"""Browser login/logout.

Separate from the intake auth in app/api/webhooks.py on purpose: Alertmanager
is a machine and carries a bearer token; operators carry a session cookie.
Mixing the two would mean a leaked intake token grants admin access.
"""

import logging

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security import SESSION_USER_KEY, verify_password
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


@router.get("/login")
async def login_form(request: Request):
    if request.session.get(SESSION_USER_KEY):
        return RedirectResponse("/alerts", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "ui/login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()

    # One message for every failure mode (unknown user, wrong password,
    # deactivated account) so the form cannot be used to enumerate accounts.
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        log.warning("failed login for username=%r", username)
        return templates.TemplateResponse(
            request,
            "ui/login.html",
            {"error": "Invalid username or password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Drop any pre-login session state before elevating (session fixation).
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    log.info("login: %s (%s)", user.username, user.role.value)
    return RedirectResponse("/alerts", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
