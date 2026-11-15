import re

from fastapi.responses import RedirectResponse

from backend.config import settings

# The browser keeps its session ID in this cookie. Streamlit reads it on every run, so a connection
# survives page refreshes and the OAuth redirect back to the frontend.
SESSION_COOKIE_NAME = "troubleshooting_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def is_valid_session_id(value: str) -> bool:
    return bool(_SESSION_ID_PATTERN.fullmatch(value or ""))


def redirect_with_session(url: str, session_id: str) -> RedirectResponse:
    response = RedirectResponse(url=url)
    if not is_valid_session_id(session_id):
        return response
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.frontend_url.startswith("https://"),
        path="/",
    )
    return response
