from __future__ import annotations

import argparse
import base64
import getpass
import html as ihtml
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import msal
import requests
import urllib3
from dotenv import load_dotenv


# =========================
# .env loading (script/exe folder)
# =========================
def _base_dir() -> Path:
    # Works for normal python + PyInstaller frozen exe
    if getattr(sys, "frozen", False) and hasattr(sys, "executable"):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


load_dotenv(dotenv_path=_base_dir() / ".env", override=False)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# =========================
# OneNote (Microsoft Graph)
# =========================
TENANT_ID = (os.getenv("AZURE_TENANT_ID") or "").strip()
CLIENT_ID = (os.getenv("AZURE_CLIENT_ID") or "").strip()

SECTION_NAME = (os.getenv("ONENOTE_SECTION_NAME") or "CFE staff").strip()
SECTION_ID = (os.getenv("ONENOTE_SECTION_ID") or "1-03f44d4c-a88a-418f-a72c-bb3afec2d7b5").strip()

DEFAULT_PAGE_TITLE = (os.getenv("DEFAULT_PAGE_TITLE") or "").strip()

# Optional: local VLM endpoint for image text/caption extraction (e.g., LLaVA, Qwen-VL, Llama-Vision)
VLM_BASE_URL = (os.getenv("VLM_BASE_URL") or "").strip().rstrip("/")
VLM_MODEL = (os.getenv("VLM_MODEL") or "").strip()
VLM_API_KEY = (os.getenv("VLM_API_KEY") or "").strip()
VLM_TIMEOUT = int((os.getenv("VLM_TIMEOUT") or "90").strip())
VLM_OCR_MAX_TOKENS = int((os.getenv("VLM_OCR_MAX_TOKENS") or "800").strip())
VLM_MAX_IMAGES = int((os.getenv("VLM_MAX_IMAGES") or "0").strip())

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_TIMEOUT = 30

# Must match Redirect URI configured in Entra App Registration (Web platform)
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8400
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/getToken"


def _graph_token_cache_path() -> Path:
    configured = (os.getenv("GRAPH_TOKEN_CACHE_FILE") or "").strip()
    if configured:
        return Path(configured)
    return _base_dir() / ".graph_msal_token_cache.json"


def _load_msal_token_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    path = _graph_token_cache_path()
    try:
        if path.exists():
            cache.deserialize(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[Auth] Warning: failed to read token cache {path}: {exc}")
    return cache


def _save_msal_token_cache(cache: msal.SerializableTokenCache) -> None:
    if not getattr(cache, "has_state_changed", False):
        return
    path = _graph_token_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.serialize(), encoding="utf-8")
    except Exception as exc:
        print(f"[Auth] Warning: failed to write token cache {path}: {exc}")


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _split_emails(csv: str) -> List[str]:
    out: List[str] = []
    for x in (csv or "").split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out


# =========================
# Recipients config
# =========================
RECIPIENTS_FILE_DEFAULT = "recipients.json"
DEFAULT_TO = _split_emails(os.getenv("DEFAULT_TO", ""))
DEFAULT_CC = _split_emails(os.getenv("DEFAULT_CC", ""))


def load_recipients(path: str) -> Tuple[List[str], List[str]]:
    """
    Loads recipients from JSON file:
      { "to": ["a@b.com"], "cc": ["c@d.com"] }

    Falls back to DEFAULT_TO / DEFAULT_CC if file missing or invalid.
    """
    p = Path(path)
    if not p.exists():
        return DEFAULT_TO.copy(), DEFAULT_CC.copy()

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        to_list = data.get("to", [])
        cc_list = data.get("cc", [])

        if isinstance(to_list, str):
            to_list = _split_emails(to_list)
        if isinstance(cc_list, str):
            cc_list = _split_emails(cc_list)

        to_list = [str(x).strip() for x in to_list if str(x).strip()]
        cc_list = [str(x).strip() for x in cc_list if str(x).strip()]

        return (to_list or DEFAULT_TO.copy()), (cc_list or DEFAULT_CC.copy())
    except Exception:
        return DEFAULT_TO.copy(), DEFAULT_CC.copy()


# =========================
# Credential prompting
# =========================
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _warn_if_looks_wrong(name: str, value: str) -> None:
    # Heuristics only; do not block execution
    if _GUID_RE.match(value):
        print(
            f"[Warn] {name} looks like a GUID. "
            "Azure client secret VALUE is usually not a GUID. "
            "You may have pasted the *Secret ID* instead of the *Value*."
        )
    if len(value) < 20:
        print(f"[Warn] {name} is short ({len(value)} chars). That’s unusual for a secret/token.")


def _prompt_secret(label: str, *, min_len: int = 20) -> str:
    """
    cmd.exe + getpass() often breaks Ctrl+V paste (becomes '\\x16' and you capture 1 char).
    Strategy:
      1) Try hidden getpass() when stdin is a TTY.
      2) If it looks like paste failed (too short, control char), fall back to visible input().
    Notes:
      - In hidden mode you WILL NOT see characters while typing/pasting (that's normal).
      - In visible fallback, Ctrl+V works reliably.
    """

    def looks_bad(s: str) -> bool:
        s = s or ""
        if s == "\x16":  # Ctrl+V control char in cmd.exe hidden prompt
            return True
        if any(ord(ch) < 32 for ch in s):  # other control chars
            return True
        return len(s.strip()) < min_len

    # Try hidden input first when possible
    if sys.stdin is not None and sys.stdin.isatty():
        print(f"[Input] {label} (hidden). Paste tips in cmd.exe: try Right-click or Shift+Insert (Ctrl+V may fail).")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # suppress GetPassWarning in odd consoles
            s = getpass.getpass("> ").strip()

        print(f"[Input] Captured {len(s)} characters.")
        if not looks_bad(s):
            return s

        print("[Warn] Hidden input paste likely failed (cmd.exe Ctrl+V often becomes 1 char).")

    # Visible fallback
    print("[Input] Falling back to VISIBLE input so paste works (yes, it will be shown).")
    s = input(f"{label} (VISIBLE): ").strip()
    print(f"[Input] Captured {len(s)} characters.")
    if looks_bad(s):
        raise RuntimeError(f"{label} looks invalid/too short. Paste the full VALUE (not secret ID).")
    return s


def resolve_graph_client_secret() -> str:
    """
    Priority:
    1) env GRAPH_CLIENT_SECRET (loaded from .env)
    2) prompt
    """
    env_val = (os.getenv("GRAPH_CLIENT_SECRET") or "").strip()
    if env_val:
        print("[Config] Using GRAPH_CLIENT_SECRET from environment/.env.")
        _warn_if_looks_wrong("GRAPH_CLIENT_SECRET", env_val)
        return env_val

    val = _prompt_secret("Paste Azure CLIENT SECRET VALUE (NOT secret ID)", min_len=20)
    _warn_if_looks_wrong("Azure client secret", val)
    return val


def resolve_gnai_token() -> str:
    """
    Priority:
    1) env GNAI_TOKEN (loaded from .env)
    2) env EXPERTGPT_TOKEN (backward compatibility)
    3) prompt
    """
    env_val = (os.getenv("GNAI_TOKEN") or "").strip()
    if env_val:
        print("[Config] Using GNAI_TOKEN from environment/.env.")
        _warn_if_looks_wrong("GNAI_TOKEN", env_val)
        return env_val

    fallback = (os.getenv("EXPERTGPT_TOKEN") or "").strip()
    if fallback:
        print("[Config] Using EXPERTGPT_TOKEN fallback from environment/.env.")
        _warn_if_looks_wrong("EXPERTGPT_TOKEN", fallback)
        return fallback

    val = _prompt_secret("Paste GNAI token", min_len=20)
    _warn_if_looks_wrong("GNAI token", val)
    return val


# ---------- HTML -> Plain text ----------
class _OneNoteHTMLToText(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "thead", "tbody", "tr",
        "ul", "ol",
    }
    LINEBREAK_TAGS = {"br"}
    CELL_TAGS = {"td", "th"}
    LIST_ITEM_TAGS = {"li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: List[str] = []
        self._in_script_style = False

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in ("script", "style"):
            self._in_script_style = True
            return
        if tag in self.LINEBREAK_TAGS:
            self.parts.append("\n")
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag in self.CELL_TAGS:
            self.parts.append("\t")
        if tag in self.LIST_ITEM_TAGS:
            self.parts.append("\n- ")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in ("script", "style"):
            self._in_script_style = False
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag in ("tr",):
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self._in_script_style or not data:
            return
        self.parts.append(data)

    def handle_entityref(self, name: str):
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str):
        self.parts.append(f"&#{name};")

    def get_text(self) -> str:
        raw = "".join(self.parts)
        raw = ihtml.unescape(raw)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s+\n", "\n\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_plain_text(html: str) -> str:
    parser = _OneNoteHTMLToText()
    parser.feed(html)
    parser.close()
    return parser.get_text()


class _ImageSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.urls: List[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "img":
            return
        for k, v in attrs:
            if k in ("data-render-src", "src") and v:
                self.urls.append(v)


def extract_image_sources(html: str) -> List[str]:
    parser = _ImageSrcParser()
    parser.feed(html)
    parser.close()
    return parser.urls


# ---------- Minimal local server to capture auth code ----------
class _AuthCodeHandler(BaseHTTPRequestHandler):
    server_version = "AuthCodeCatcher/1.0"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/getToken":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        qs = urllib.parse.parse_qs(parsed.query)
        self.server.auth_response_qs = {k: v[0] for k, v in qs.items()}  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h3>Login complete.</h3>You can close this tab and return to the script.</body></html>"
        )

    def log_message(self, format, *args):
        return


def _wait_for_auth_response(timeout_sec: int = 180) -> Dict[str, str]:
    httpd = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _AuthCodeHandler)
    httpd.auth_response_qs = None  # type: ignore[attr-defined]

    def serve():
        httpd.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    resp = getattr(httpd, "auth_response_qs", None)
    if not resp:
        raise TimeoutError("Timed out waiting for login redirect back to localhost.")
    return resp


# ---------- Auth (Delegated via Authorization Code + client secret) ----------
def get_graph_token_delegated_with_secret(client_secret: str, scopes: Optional[List[str]] = None) -> str:
    scopes = scopes or ["Notes.Read", "Mail.Send"]

    if not client_secret.strip():
        raise RuntimeError("Client secret is empty.")

    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    token_cache = _load_msal_token_cache()
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        authority=authority,
        client_credential=client_secret.strip(),
        token_cache=token_cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes=scopes, account=accounts[0])
        if result and "access_token" in result:
            print("[Auth] Using cached Graph delegated token.")
            _save_msal_token_cache(token_cache)
            return result["access_token"]

    flow = app.initiate_auth_code_flow(scopes=scopes, redirect_uri=REDIRECT_URI)
    auth_url = flow["auth_uri"]

    print("[Auth] Opening browser for sign-in...")
    print(f"[Auth] If it doesn't open, paste this URL into a browser:\n{auth_url}\n")
    webbrowser.open(auth_url, new=1, autoraise=True)

    auth_response = _wait_for_auth_response(timeout_sec=180)
    result = app.acquire_token_by_auth_code_flow(flow, auth_response)
    _save_msal_token_cache(token_cache)

    if "access_token" not in result:
        raise RuntimeError(f"Token error: {result}")
    return result["access_token"]


def get_graph_token_app_only(client_secret: str, scopes: Optional[List[str]] = None) -> str:
    scopes = scopes or ["https://graph.microsoft.com/.default"]

    if not client_secret.strip():
        raise RuntimeError("Client secret is empty.")

    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        authority=authority,
        client_credential=client_secret.strip(),
    )

    result = app.acquire_token_for_client(scopes=scopes)
    if "access_token" not in result:
        raise RuntimeError(f"App-only token error: {result}")
    return result["access_token"]


# ---------- Graph helpers ----------
_SESSION = requests.Session()


def graph_get_json(token: str, url: str, *, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    headers = _auth_headers(token)
    if extra_headers:
        headers = {**headers, **extra_headers}
    r = _SESSION.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()


def graph_get_all(token: str, url: str, *, extra_headers: Optional[Dict[str, str]] = None) -> List[dict]:
    items: List[dict] = []
    while url:
        data = graph_get_json(token, url, extra_headers=extra_headers)
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


def find_page_in_section(token: str, section_id: str, page_title: str) -> Optional[Tuple[str, str]]:
    title = (page_title or "").strip()
    if not title:
        return None

    # 1) FAST: server-side exact match via $filter
    try:
        lower = title.lower().replace("'", "''")
        url = (
            f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
            f"?$select=id,title"
            f"&$top=1"
            f"&$filter=tolower(title) eq '{lower}'"
        )
        data = graph_get_json(token, url)
        vals = data.get("value", []) or []
        if vals:
            p = vals[0]
            return p.get("id"), (p.get("title") or title)
    except Exception:
        pass

    # 2) FAST-ish: $search fallback
    try:
        q = title.replace('"', '\\"')
        url = (
            f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
            f"?$select=id,title"
            f"&$top=25"
            f"&$search=\"title:{q}\""
        )
        data = graph_get_json(token, url, extra_headers={"ConsistencyLevel": "eventual"})
        vals = data.get("value", []) or []
        target = title.lower()
        for p in vals:
            t = (p.get("title") or "").strip()
            if t.lower() == target:
                return p.get("id"), t
    except Exception:
        pass

    # 3) SLOW: enumerate all pages
    url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages?$select=id,title"
    pages = graph_get_all(token, url)
    target = title.lower()
    for p in pages:
        t = (p.get("title") or "").strip()
        if t.lower() == target:
            return p.get("id"), t
    return None


def get_latest_page_in_section(token: str, section_id: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """Return (page_id, title, createdDateTime) for the newest page in the section."""
    url = (
        f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
        f"?$select=id,title,createdDateTime"
        f"&$orderby=createdDateTime desc"
        f"&$top=1"
    )

    try:
        data = graph_get_json(token, url)
        vals = data.get("value", []) or []
        if not vals:
            return None
        p = vals[0]
        return p.get("id"), (p.get("title") or "").strip(), p.get("createdDateTime")
    except Exception:
        return None


def get_page_html(token: str, page_id: str) -> str:
    url = f"{GRAPH_BASE}/me/onenote/pages/{page_id}/content"
    r = _SESSION.get(url, headers=_auth_headers(token), timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.text


def fetch_image_bytes(token: str, url: str) -> bytes:
    r = _SESSION.get(url, headers=_auth_headers(token), timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.content


def _guess_image_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _build_vlm_chat_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def vlm_image_to_text(image_bytes: bytes) -> str:
    if not VLM_BASE_URL or not VLM_MODEL:
        return ""

    mime = _guess_image_mime(image_bytes)
    img_b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": VLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an OCR transcriber. Return only the text visible in the image. "
                    "Do not summarize, translate, or infer. Preserve line breaks and bullet structure."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Transcribe all readable text exactly as shown. "
                            "Keep original reading order, line breaks, and list markers. "
                            "For unreadable words, use [?]. "
                            "If there is no readable text, return exactly: [NO_TEXT]"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{img_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": VLM_OCR_MAX_TOKENS,
    }

    headers = {"Content-Type": "application/json"}
    if VLM_API_KEY:
        headers["Authorization"] = f"Bearer {VLM_API_KEY}"

    url = _build_vlm_chat_url(VLM_BASE_URL)
    resp = requests.post(url, headers=headers, json=payload, timeout=VLM_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    out = (choices[0].get("message", {}).get("content") or "").strip()
    if out == "[NO_TEXT]":
        return ""
    return out


def extract_and_ocr_images(token: str, html: str) -> List[str]:
    urls = extract_image_sources(html)
    if not urls:
        return []

    limit = max(0, VLM_MAX_IMAGES)
    if limit and len(urls) > limit:
        print(f"[OCR] Found {len(urls)} image(s); limiting OCR to first {limit} for speed.")
        urls = urls[:limit]
    else:
        print(f"[OCR] Found {len(urls)} image(s); sending to VLM at {VLM_BASE_URL or '[unset]'}...")

    ocr_blocks: List[str] = []
    success_count = 0
    no_text_count = 0
    failed_count = 0
    total = len(urls)

    def render_progress(current: int) -> str:
        width = 20
        filled = 0 if total <= 0 else int((current / total) * width)
        filled = max(0, min(width, filled))
        return f"[{'#' * filled}{'-' * (width - filled)}] {current}/{total}"

    for idx, url in enumerate(urls, start=1):
        print(f"[OCR] {render_progress(idx - 1)} Starting image {idx}/{total}...")
        started_at = time.perf_counter()
        try:
            img_bytes = fetch_image_bytes(token, url)
            text = vlm_image_to_text(img_bytes)
            elapsed = time.perf_counter() - started_at
            if text:
                ocr_blocks.append(f"[Image {idx}]\n{text}")
                success_count += 1
                print(
                    f"[OCR] {render_progress(idx)} Image {idx}/{total} done in {elapsed:.1f}s "
                    f"| ok={success_count} no_text={no_text_count} failed={failed_count}"
                )
            else:
                no_text_count += 1
                print(
                    f"[OCR] {render_progress(idx)} Image {idx}/{total}: no text detected in {elapsed:.1f}s "
                    f"| ok={success_count} no_text={no_text_count} failed={failed_count}"
                )
        except Exception as e:
            elapsed = time.perf_counter() - started_at
            failed_count += 1
            print(
                f"[OCR] {render_progress(idx)} Image {idx}/{total} failed in {elapsed:.1f}s: {e} "
                f"| ok={success_count} no_text={no_text_count} failed={failed_count}"
            )

    print(
        f"[OCR] Completed image OCR: total={total} ok={success_count} "
        f"no_text={no_text_count} failed={failed_count}"
    )

    return ocr_blocks


def send_mail_via_graph(
    token: str,
    subject: str,
    body_text: str,
    to_addrs: List[str],
    cc_addrs: Optional[List[str]] = None,
    save_to_sent_items: bool = True,
    content_type: str = "Text",
    sender_upn: Optional[str] = None,
) -> None:
    sender = (sender_upn or "").strip()
    if sender:
        sender_escaped = urllib.parse.quote(sender, safe="")
        url = f"{GRAPH_BASE}/users/{sender_escaped}/sendMail"
    else:
        url = f"{GRAPH_BASE}/me/sendMail"

    normalized_content_type = "HTML" if str(content_type or "").strip().upper() == "HTML" else "Text"

    payload: Dict[str, Any] = {
        "message": {
            "subject": subject,
            "body": {"contentType": normalized_content_type, "content": body_text},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to_addrs],
        },
        "saveToSentItems": save_to_sent_items,
    }

    if cc_addrs:
        payload["message"]["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc_addrs]

    r = _SESSION.post(
        url,
        headers={**_auth_headers(token), "Content-Type": "application/json"},
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )

    if r.status_code not in (202, 200):
        raise RuntimeError(f"Graph sendMail failed: {r.status_code} {r.text}")


# =========================
# GNAI (Intel internal OpenAI-compatible provider)
# =========================
GNAI_URL = os.getenv(
    "GNAI_URL",
    os.getenv("EXPERTGPT_URL", "https://gnai.intel.com/api/providers/openai/v1"),
).rstrip("/")
GNAI_MODEL = (os.getenv("GNAI_MODEL") or os.getenv("EXPERTGPT_MODEL") or os.getenv("MODEL") or "gpt-4.1").strip()
GNAI_INSECURE = (os.getenv("GNAI_INSECURE") or os.getenv("EXPERTGPT_INSECURE") or "").strip().lower() in {"1", "true", "yes"}

SYSTEM_CONTENT = """You are a highly capable assistant.
You will be given plain-text content extracted from a OneNote page.
Your job:
- Summarize clearly for a busy engineer.
- Preserve key names, dates, action items, decisions, risks, and open questions.
- Use concise bullets and headings.
- If content is meeting notes, include: Decisions, Action Items (with owners if present), Risks/Blockers, Next Steps.
"""

# Keep warning suppression since some internal setups use custom cert chains.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class GNAIClient:
    def __init__(self, token: str):
        self.token = (token or "").strip()
        if not self.token:
            raise RuntimeError("GNAI token is empty.")

        self.client = httpx.Client(
            base_url=GNAI_URL,
            verify=not GNAI_INSECURE,
            trust_env=True,
            timeout=60,
        )

    def close(self) -> None:
        """Close the HTTP client and release resources."""
        if hasattr(self, 'client'):
            self.client.close()

    def chat(self, user_content: str, max_tokens: int = 1500) -> str:
        payload = {
            "model": GNAI_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_CONTENT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "top_p": 0.9,
            "frequency_penalty": 0.1,
            "presence_penalty": 0,
            "max_tokens": max_tokens,
        }

        r = self.client.post(
            "/chat/completions",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            json=payload,
        )

        if r.status_code == 401:
            raise RuntimeError(f"GNAI 401 Unauthorized: {r.text}")
        r.raise_for_status()

        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


def chunk_text(text: str, max_chars: int = 12000) -> List[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        cut = text.rfind("\n", start, end)
        if cut <= start + 200:
            cut = end
        chunks.append(text[start:cut].strip())
        start = cut

    return [c for c in chunks if c]


def build_summary_prompt(section_name: str, page_title: str, page_text: str) -> str:
    return f"""Summarize the following OneNote page content.

Section: {section_name}
Page Title: {page_title}

Requirements:
- Output in Markdown
- Start with a 1-paragraph executive summary
- Then bullet sections:
  - Key Points
  - Decisions
  - Action Items (owner, due date if present)
  - Risks/Blockers
  - Open Questions
- If any area is not present in the notes, write "None".

CONTENT:
\"\"\"
{page_text}
\"\"\"
"""


# =========================
# CLI + Main
# =========================
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="OneNote -> GNAI summary -> Send via Graph (from signed-in user)."
    )
    p.add_argument("--send-email", action="store_true", help="Actually send the email via Graph.")
    p.add_argument("--recipients-file", default=RECIPIENTS_FILE_DEFAULT, help="JSON file with to/cc lists.")
    p.add_argument("--to", default="", help="Override To (comma-separated). If empty, uses recipients file.")
    p.add_argument("--cc", default="", help="Override Cc (comma-separated). If empty, uses recipients file.")
    p.add_argument("--attach-files", action="store_true", help="Save HTML/text/summary outputs to disk.")
    p.add_argument("--dry-run-email", action="store_true", help="Print final to/cc/subject and exit (no send).")
    p.add_argument("--page-title", default=DEFAULT_PAGE_TITLE, help="OneNote page title to fetch. If empty, prompt.")
    return p.parse_args(argv)


def resolve_recipients(args) -> Tuple[List[str], List[str]]:
    to_list, cc_list = load_recipients(args.recipients_file)
    if args.to.strip():
        to_list = _split_emails(args.to)
    if args.cc.strip():
        cc_list = _split_emails(args.cc)
    if not to_list:
        to_list = DEFAULT_TO.copy()
    return to_list, cc_list


def main() -> int:
    args = parse_args()
    llm_client: Optional[GNAIClient] = None

    try:
        # Validate required configuration
        if not TENANT_ID:
            raise RuntimeError("AZURE_TENANT_ID not configured. Set it in .env file.")
        if not CLIENT_ID:
            raise RuntimeError("AZURE_CLIENT_ID not configured. Set it in .env file.")

        graph_secret = resolve_graph_client_secret()
        gnai_token = resolve_gnai_token()

        page_title = (args.page_title or DEFAULT_PAGE_TITLE).strip()

        token = get_graph_token_delegated_with_secret(graph_secret, scopes=["Notes.Read", "Mail.Send"])

        print(f"[Lookup] Using section id: {SECTION_ID}")

        page_id: Optional[str] = None
        real_title: Optional[str] = None

        if page_title:
            print(f"[Lookup] Searching page title: {page_title}")
            found = find_page_in_section(token, SECTION_ID, page_title)
            if not found:
                print(f"[Error] Page titled '{page_title}' not found in section id '{SECTION_ID}'.")
                return 1
            page_id, real_title = found
            print(f"[Lookup] Page found: '{real_title}' (id: {page_id})")
        else:
            latest = get_latest_page_in_section(token, SECTION_ID)
            if not latest:
                print(f"[Error] No pages found in section id '{SECTION_ID}'.")
                return 1
            page_id, real_title, created_dt = latest
            created_msg = f" created at {created_dt}" if created_dt else ""
            print(f"[Lookup] No page title provided. Using latest page: '{real_title}' (id: {page_id}){created_msg}")

        if not page_id or not real_title:
            raise RuntimeError("Page lookup failed to return id/title.")

        html = get_page_html(token, page_id)
        page_text = html_to_plain_text(html)

        if not page_text.strip():
            print("[Warn] Extracted page text is empty after HTML->text conversion.")
            return 1

        ocr_blocks = extract_and_ocr_images(token, html)
        if ocr_blocks:
            page_text = page_text + "\n\n[Image OCR]\n" + "\n\n".join(ocr_blocks)

        if args.attach_files:
            with open("onenote_page.html", "w", encoding="utf-8") as f:
                f.write(html)
            with open("onenote_page.txt", "w", encoding="utf-8") as f:
                f.write(page_text)
            print("[OK] Saved: onenote_page.html, onenote_page.txt")

        llm_client = GNAIClient(gnai_token)

        chunks = chunk_text(page_text, max_chars=12000)
        if len(chunks) == 1:
            prompt = build_summary_prompt(SECTION_NAME, real_title, chunks[0])
            summary = llm_client.chat(prompt, max_tokens=1500)
        else:
            print(f"[Info] Large page. Summarizing in {len(chunks)} chunks...")
            partials: List[str] = []
            for i, ch in enumerate(chunks, start=1):
                prompt = build_summary_prompt(SECTION_NAME, f"{real_title} (chunk {i}/{len(chunks)})", ch)
                partial = llm_client.chat(prompt, max_tokens=1200)
                partials.append(f"## Chunk {i}\n{partial}".strip())

            combine_prompt = f"""You will be given multiple chunk summaries from a single OneNote page.
Combine them into ONE final summary with the same required structure.

Section: {SECTION_NAME}
Page Title: {real_title}

CHUNK SUMMARIES:
\"\"\"
{'\n\n'.join(partials)}
\"\"\"
"""
            summary = llm_client.chat(combine_prompt, max_tokens=1500)

        with open("onenote_summary.md", "w", encoding="utf-8") as f:
            f.write(summary)
        print("[OK] Saved summary to: onenote_summary.md")

        print("\n=== Page Summary (GNAI) ===\n")
        print(summary)

        to_addrs, cc_addrs = resolve_recipients(args)
        subject = f"OneNote Summary: {SECTION_NAME} / {real_title}"

        if args.dry_run_email:
            print("\n[DRY RUN] Would email:")
            print("  Subject:", subject)
            print("  To     :", ", ".join(to_addrs))
            print("  Cc     :", ", ".join(cc_addrs) if cc_addrs else "(none)")
            return 0

        if args.send_email:
            send_mail_via_graph(
                token=token,
                subject=subject,
                body_text=summary,
                to_addrs=to_addrs,
                cc_addrs=cc_addrs or None,
                save_to_sent_items=True,
            )
            print(
                f"\n[OK] Email sent via Graph to: {', '.join(to_addrs)}"
                + (f" (cc: {', '.join(cc_addrs)})" if cc_addrs else "")
            )
        else:
            print("\n[Info] Not sending email (missing --send-email).")
            print(f"[Info] Resolved recipients would be To={to_addrs} Cc={cc_addrs}")

        return 0

    except requests.HTTPError as e:
        resp = e.response
        print(f"[HTTP Error] {resp.status_code}: {(resp.text or '')[:2000]}")
        return 2
    except Exception as e:
        print(f"[Error] {e}")
        return 3
    finally:
        # Clean up session resources
        _SESSION.close()
        if llm_client is not None:
            llm_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
