import base64
import hashlib
import hmac
import json
import random
import string
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

API_BASE = "https://apis.reolink.com"
LIST_URL = f"{API_BASE}/v2/cloud/event/items/list-query/"
SESSION_URL = f"{API_BASE}/v1.0/oauth2/session/details/"
TOKEN_URL = f"{API_BASE}/v1.0/oauth2/token/"
MFA_CODES_URL = f"{API_BASE}/v2/auth/mfa/codes"
PROFILE_URL = f"{API_BASE}/v1.0/users/@me/profile/"

ORIGIN = "https://cloud.reolink.com"
REFERER = "https://cloud.reolink.com/"
# my.reolink.com is the account-center origin used for the password grant.
ACCOUNT_ORIGIN = "https://my.reolink.com"
ACCOUNT_REFERER = "https://my.reolink.com/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)

# client_id the cloud.reolink.com web client uses for the token exchange.
CLIENT_ID = "REO-B<c[n0Q27RPZPc,Si1]v"
# client_id the my.reolink.com account center uses for the password grant.
PASSWORD_CLIENT_ID = "REO-.AJ,HO/L6_TG44T78KB7"
# MFA verification scenario for a username/password login.
LOGIN_SCENARIO = "users.login_with_password"


def login(code: str) -> str:
    """Exchange a Reolink session code for a bearer access token.

    Reproduces the web client's ``POST /v1.0/oauth2/token/`` call using the
    ``session_implicit`` grant. ``code`` is the durable session credential
    (Reolink reports it as non-expiring); the returned access token is a bearer
    token valid for ~30 minutes, suitable for ``ReolinkCloud(token=...)``.
    """
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "session_implicit",
            "code": code,
        },
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": ORIGIN,
            "Referer": REFERER,
            "User-Agent": USER_AGENT,
            "X-PRID": make_prid(TOKEN_URL),
        },
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()
    token = payload.get("access_token")

    if not token:
        raise RuntimeError(f"Reolink login did not return a token: {payload}")

    return token


def totp(
    secret: str,
    *,
    digits: int = 6,
    period: int = 30,
    digest=hashlib.sha1,
    at: float | None = None,
) -> str:
    """Return the current TOTP code for a base32 secret (RFC 6238)."""
    key = base64.b32decode(secret.replace(" ", "").upper())
    counter = int((time.time() if at is None else at) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def _password_grant(
    email: str,
    password: str,
    *,
    verify_id: str | None = None,
    verify_code: str | None = None,
) -> requests.Response:
    """POST the ``grant_type=password`` token request (optionally with MFA)."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": ACCOUNT_ORIGIN,
        "Referer": ACCOUNT_REFERER,
        "User-Agent": USER_AGENT,
        "X-PRID": make_prid(TOKEN_URL),
        "X-Verify-Scenario": LOGIN_SCENARIO,
    }

    if verify_id and verify_code:
        headers["X-Verify-Id"] = verify_id
        headers["X-Verify-Code"] = verify_code

    return requests.post(
        TOKEN_URL,
        data={
            "username": email,
            "password": password,
            "grant_type": "password",
            "session_mode": "true",
            "client_id": PASSWORD_CLIENT_ID,
            "mfa_trusted": "false",
        },
        headers=headers,
        timeout=30,
    )


def request_totp_challenge(email: str) -> str:
    """Ask Reolink to open a TOTP MFA challenge and return its verify id."""
    response = requests.post(
        MFA_CODES_URL,
        json={
            "clientId": PASSWORD_CLIENT_ID,
            "scenario": LOGIN_SCENARIO,
            "method": "totp",
            "data": {"account": f"email:{email}"},
        },
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": ACCOUNT_ORIGIN,
            "Referer": ACCOUNT_REFERER,
            "User-Agent": USER_AGENT,
            "X-PRID": make_prid(MFA_CODES_URL),
        },
        timeout=30,
    )

    response.raise_for_status()

    verify_id = response.json().get("id")

    if not verify_id:
        raise RuntimeError(
            f"Reolink did not return an MFA challenge id: {response.text}"
        )

    return verify_id


def password_login(
    email: str,
    password: str,
    totp_secret: str,
) -> str:
    """Log in with email/password (+ TOTP MFA) and return the web session code.

    Reproduces the my.reolink.com account-center flow: a ``grant_type=password``
    token request, and — when the account requires it — a TOTP second factor
    submitted via the ``X-Verify-*`` headers. The returned
    ``web_session_auth_code`` is the durable (Reolink reports it non-expiring)
    session credential consumed by :func:`login`.
    """
    response = _password_grant(email, password)

    if response.status_code != 200:
        error = response.json().get("error", {})

        if error.get("symbol") != "mfa_required":
            raise RuntimeError(f"Reolink password login failed: {response.text}")

        verify_id = request_totp_challenge(email)

        response = _password_grant(
            email,
            password,
            verify_id=verify_id,
            verify_code=totp(totp_secret),
        )
        response.raise_for_status()

    code = response.json().get("web_session_auth_code")

    if not code:
        raise RuntimeError(
            f"Reolink login did not return a web session code: {response.text}"
        )

    return code


def fetch_user_id(token: str) -> str:
    """Return the Reolink account id for a bearer access token."""
    response = requests.get(
        PROFILE_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": ORIGIN,
            "Referer": REFERER,
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
            "X-PRID": make_prid(PROFILE_URL),
        },
        timeout=30,
    )

    response.raise_for_status()

    user_id = response.json().get("id")

    if not user_id:
        raise RuntimeError(
            f"Reolink profile did not include an account id: {response.text}"
        )

    return str(user_id)


def parse_time(value: str) -> int:
    if value.isdigit():
        n = int(value)
        return n if n > 10_000_000_000 else n * 1000

    dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.astimezone()

    return int(dt.timestamp() * 1000)


def make_prid(url: str) -> str:
    now = datetime.now().strftime("%y%m%d%H%M%S%f")[:15]
    digest = hashlib.md5(url.encode()).hexdigest()[:8]
    suffix = "".join(
        random.choice(string.ascii_letters + string.digits) for _ in range(7)
    )
    return f"{now}-{digest}-{suffix}"


def decrypt_stm_response(
    response: dict[str, Any],
    user_id: str,
) -> dict[str, Any]:
    """
    Reolink STM v1 response encryption found in the web client:

        HMAC-SHA256(
            message = user_id,
            key     = response["time"]
        )

        key = first 16 bytes
        iv  = next 16 bytes

        AES-128-CFB, full-block CFB, no padding
    """
    if not response.get("stm"):
        return response

    if response["stm"] != 1:
        raise ValueError(f"Unsupported STM version: {response['stm']}")

    digest = hmac.new(
        response["time"].encode(),
        user_id.encode(),
        hashlib.sha256,
    ).digest()

    key = digest[:16]
    iv = digest[16:32]

    ciphertext = base64.b64decode(response["data"])

    decryptor = Cipher(
        algorithms.AES(key),
        modes.CFB(iv),
    ).decryptor()

    plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    return json.loads(plaintext.decode("utf-8"))


def make_access_key(item_id: str, ck: str) -> str:
    """
    Reolink file-dl authorization:

        HMAC-SHA256(
            message = item_id,
            key     = ck
        )

    encoded as URL-safe base64 without '=' padding.
    """
    digest = hmac.new(
        ck.encode(),
        item_id.encode(),
        hashlib.sha256,
    ).digest()

    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class ReolinkCloud:
    def __init__(
        self,
        token: str,
        user_id: str,
        session_code: str | None = None,
    ):
        self.session = requests.Session()

        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": ORIGIN,
                "Referer": REFERER,
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token}",
            }
        )

        self.user_id = user_id
        # Durable web session code, kept so an expired bearer (~30 min) can be
        # re-minted mid-run without redoing the password/MFA login.
        self.session_code = session_code

    def _refresh_token(self) -> None:
        """Mint a fresh bearer from the stored session code."""
        if not self.session_code:
            raise RuntimeError(
                "Bearer token expired and no session code is available to refresh it"
            )
        token = login(self.session_code)
        self.session.headers["Authorization"] = f"Bearer {token}"

    @classmethod
    def from_session_code(
        cls,
        code: str,
        user_id: str,
    ) -> "ReolinkCloud":
        """Log in with a session code and return an authenticated client."""
        return cls(token=login(code), user_id=user_id, session_code=code)

    @classmethod
    def from_credentials(
        cls,
        email: str,
        password: str,
        totp_secret: str,
    ) -> "ReolinkCloud":
        """Log in with email/password (+ TOTP MFA) and return a client.

        Runs the full account-center sign-in — password grant, TOTP second
        factor, ``session_implicit`` token exchange — and resolves the account
        id, so no session code or user id has to be supplied by hand.
        """
        code = password_login(email, password, totp_secret)
        token = login(code)
        return cls(token=token, user_id=fetch_user_id(token), session_code=code)

    def _list_events(
        self,
        start_ms: int,
        end_ms: int,
        next_token: str | None = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "startAt": start_ms,
            "endAt": end_ms,
            "timeType": "recordedAt",
            "sortOrder": "asc",
            "limit": limit,
            "v": 2,
        }

        if next_token:
            payload["nextToken"] = next_token

        def post() -> requests.Response:
            return self.session.post(
                LIST_URL,
                json=payload,
                headers={
                    "X-PRID": make_prid(LIST_URL),
                    "Content-Type": "application/json;charset=UTF-8",
                },
                timeout=30,
            )

        response = post()

        # The bearer expires after ~30 min; refresh once and retry on 401.
        if response.status_code == 401 and self.session_code:
            self._refresh_token()
            response = post()

        response.raise_for_status()

        return decrypt_stm_response(
            response.json(),
            self.user_id,
        )

    def iter_videos(
        self,
        start_ms: int,
        end_ms: int,
    ):
        next_token = None

        while True:
            page = self._list_events(
                start_ms=start_ms,
                end_ms=end_ms,
                next_token=next_token,
            )

            ck = page.get("ck")

            if not ck:
                raise RuntimeError("Reolink response did not contain ck")

            for event in page.get("items", []):
                event_id = str(event["id"])

                for item in event.get("items", []):
                    item_id = str(item["id"])

                    for file in item.get("files", []):
                        if file.get("type") != "video":
                            continue

                        if not file.get("url"):
                            continue

                        recorded_at = event.get("recordedAt")

                        if not recorded_at:
                            raise Exception("missing recorded at")

                        dt = datetime.fromtimestamp(
                            recorded_at / 1000,
                            tz=timezone.utc,
                        )

                        filename = (
                            f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}"
                            f"_{event_id}.mp4"
                        )

                        yield {
                            "filename": filename,
                            "event_id": event_id,
                            "item_id": item_id,
                            "uid": event.get("uid"),
                            "recorded_at": recorded_at,
                            "uploaded_at": file.get("uploadedAt"),
                            "size": file.get("size"),
                            "resolution": file.get("resolution"),
                            "url": file["url"],
                            "access_key": make_access_key(
                                item_id,
                                ck,
                            ),
                        }

            next_token = page.get("nextToken")

            if not next_token:
                break

    def download_video(
        self,
        video: dict[str, Any],
    ) -> Path:
        with self.session.get(
            video["url"],
            headers={
                "Authorization": None,
                "X-Access-Key": video["access_key"],
                "Referer": REFERER,
                "Origin": ORIGIN,
                "Accept": "*/*",
                "Range": "bytes=0-",
            },
            stream=True,
            timeout=120,
        ) as response:
            if response.status_code not in (200, 206):
                raise RuntimeError(
                    f"Download failed for {video['event_id']}: "
                    f"{response.status_code} {response.text[:500]}"
                )

            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk



