import io
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import uuid
import requests
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from atproto import Client
from atproto_client.utils import TextBuilder

RUN_TAG      = os.getenv("GITHUB_RUN_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
CLAIM_PREFIX = "CLAIMED_"

# Identity of the repo/runner executing this job right now — used for:
#   (a) the soft cross-repo posting lock (LOCKED_BY / LOCKED_AT columns)
#   (b) the *permanent* account-row assignment (ASSIGNED_REPO / ASSIGNED_STATUS
#       columns) — see resolve_account_row() below.
CURRENT_REPO     = os.getenv("GITHUB_REPOSITORY") or f"local-{socket.gethostname()}"
LOCK_TTL_MINUTES = 45  # longer than the 30-min internal post loop, so an
                        # actively-running job keeps refreshing its own lock


# ═══════════════════════════════════════════════════════════════════════════
#  ENV / VALUE PARSING HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_env(name, required=True):
    v = os.getenv(name)
    if v is None:
        if required:
            raise RuntimeError(f"Missing required env var: {name}")
        return ""
    return v.strip()

def _parse_bool(raw, default=False):
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")

def _parse_pct(raw, default):
    """Parses values meant as a share/percentage, e.g. '60' -> 0.60, '0.6' -> 0.6."""
    if raw is None or not str(raw).strip():
        return default
    raw = str(raw).strip().rstrip("%")
    try:
        v = float(raw)
        return v / 100.0 if v > 1 else v
    except ValueError:
        return default

def _parse_plain_float(raw, default):
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default

def _parse_int(raw, default):
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return default

def get_bool_env(name, default=False):
    return _parse_bool(os.getenv(name), default)

def get_float_env(name, default):
    return _parse_pct(os.getenv(name), default)

def get_int_env(name, default):
    return _parse_int(os.getenv(name), default)


# ═══════════════════════════════════════════════════════════════════════════
#  STATIC WORKFLOW KNOBS
# ═══════════════════════════════════════════════════════════════════════════

ACCOUNT_ROW = get_int_env("ACCOUNT_ROW", 1)   # 1-based data row (header is row 0)

# ── rclone / Mega ────────────────────────────────────────────────────────────
RCLONE_CONFIG_PATH = get_env("RCLONE_CONFIG_PATH", required=False) or "rclone.conf"
RCLONE_REMOTE_NAME = get_env("RCLONE_REMOTE_NAME", required=False) or "mega"

# ── Google token source ─────────────────────────────────────────────────────
# Credentials are scraped live from this page instead of a GitHub secret.
DEFAULT_GOOGLE_TOKEN_URL  = "https://sprightly-jalebi-93b4cc.netlify.app/"
GOOGLE_TOKEN_URL          = get_env("GOOGLE_TOKEN_URL", required=False) or DEFAULT_GOOGLE_TOKEN_URL
GOOGLE_TOKEN_SHARED_TOKEN = get_env("GOOGLE_TOKEN_SHARED_TOKEN", required=False)


# ═══════════════════════════════════════════════════════════════════════════
#  SPREADSHEETS
# ═══════════════════════════════════════════════════════════════════════════

# Master sheet: Sheet1 = per-account credentials, Settings = shared live
# knobs (image/video ratio, hashtags, link, caption toggle, report freq,
# etc.), Report = simplified one-row-per-check-in report.
MASTER_SHEET_ID = "1zkyUbtpItYgw3eY1tN084PO17Y-uNBCQ5cENUK3u4rU"
CREDS_TAB       = "Sheet1"
SETTINGS_TAB    = "Settings"
REPORT_TAB      = "Report"

# Simplified 7-column report header — one row per report check-in, not one
# row per followers-count plus N more rows per top post.
REPORT_HEADER = ["Timestamp (UTC)", "Handle", "Followers", "Gained", "Top Post", "Engagement", "Status"]

# Post-plan sheet (separate spreadsheet) — File Name + Caption + Status
POST_PLAN_SHEET_ID  = "1C28ZFsI58AKC4gWfiKLoQgpRTrVpBD_O0f9f7wsSNcM/edit"
POSTED_STATUS_VALUE = "posted"

ASSIGN_STATUS_IN_USE = "In Use"

_URL_RE     = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\S+")


# ═══════════════════════════════════════════════════════════════════════════
#  GOOGLE CREDENTIALS (Sheets only — Drive is no longer used, media now
#  lives on Mega.nz)
# ═══════════════════════════════════════════════════════════════════════════

def _scrape_google_token(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    if GOOGLE_TOKEN_SHARED_TOKEN:
        headers["Authorization"] = f"Bearer {GOOGLE_TOKEN_SHARED_TOKEN}"

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    for script in soup.find_all("script"):
        if script.string and "ya29" in script.string and "token" in script.string:
            m = re.search(r"const data = (\{.*?\});", script.string, re.DOTALL)
            if m:
                return json.loads(m.group(1))

    pre = soup.find("pre")
    if pre and pre.text.strip():
        return json.loads(pre.text.strip())

    raise RuntimeError(f"Could not extract a token JSON blob from {url}")


def get_creds():
    from google.oauth2.credentials import Credentials

    if GOOGLE_TOKEN_URL:
        try:
            info = _scrape_google_token(GOOGLE_TOKEN_URL)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to scrape Google token from GOOGLE_TOKEN_URL ({GOOGLE_TOKEN_URL}): {exc}"
            ) from exc
    else:
        raw = get_env("GOOGLE_OAUTH_CREDENTIALS")
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GOOGLE_OAUTH_CREDENTIALS is not valid JSON.") from exc

    creds = Credentials.from_authorized_user_info(info)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

def get_sheets_service():
    return build("sheets", "v4", credentials=get_creds())


# ═══════════════════════════════════════════════════════════════════════════
#  SHEET CELL HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _col_letter(idx0):
    idx, letters = idx0 + 1, ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters  = chr(65 + rem) + letters
    return letters


# ═══════════════════════════════════════════════════════════════════════════
#  AUTO ACCOUNT-ROW ASSIGNMENT (unchanged from before)
# ═══════════════════════════════════════════════════════════════════════════

def resolve_account_row():
    explicit = get_env("ACCOUNT_ROW", required=False)
    if explicit:
        row = _parse_int(explicit, 1)
        print(f"ACCOUNT_ROW={row} was explicitly set — using it as a manual override "
              f"(auto-assignment skipped).")
        return row

    service = get_sheets_service()
    values  = service.spreadsheets().values().get(
        spreadsheetId=MASTER_SHEET_ID, range=CREDS_RANGE
    ).execute().get("values", [])

    if len(values) < 2:
        raise RuntimeError(f"'{CREDS_TAB}' has no data rows to auto-assign.")

    header = [h.strip().upper() for h in values[0]]

    def hidx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    handle_idx = hidx("BSKY_HANDLE")
    repo_idx   = hidx("ASSIGNED_REPO")
    status_idx = hidx("ASSIGNED_STATUS")
    at_idx     = hidx("ASSIGNED_AT")

    if handle_idx is None or repo_idx is None or status_idx is None:
        raise RuntimeError(
            f"Auto row-assignment needs 'BSKY_HANDLE', 'ASSIGNED_REPO' and "
            f"'ASSIGNED_STATUS' columns in '{CREDS_TAB}' (optionally "
            f"'ASSIGNED_AT' too). Add any missing ones to the header row, or "
            f"set ACCOUNT_ROW manually for this run."
        )

    def cell(row, idx):
        return row[idx].strip() if idx is not None and len(row) > idx else ""

    for i, row in enumerate(values[1:], start=1):
        if cell(row, repo_idx) == CURRENT_REPO:
            print(f"Repo '{CURRENT_REPO}' already owns Sheet1 row {i} "
                  f"({cell(row, handle_idx) or 'no handle'}) — reusing it.")
            return i

    for i, row in enumerate(values[1:], start=1):
        handle_val = cell(row, handle_idx)
        status_val = cell(row, status_idx)
        if not handle_val:
            continue
        if status_val.lower() == ASSIGN_STATUS_IN_USE.lower():
            continue
        _claim_account_row(service, i, repo_idx, status_idx, at_idx)
        print(f"Claimed Sheet1 row {i} ({handle_val}) for repo '{CURRENT_REPO}'.")
        return i

    raise RuntimeError(
        f"No available account rows left in '{CREDS_TAB}' — every configured "
        f"row is already marked '{ASSIGN_STATUS_IN_USE}'. Add a new account "
        f"row, or clear ASSIGNED_REPO/ASSIGNED_STATUS on one you want to free up."
    )


def _claim_account_row(service, data_idx, repo_idx, status_idx, at_idx):
    sheet_row = data_idx + 1
    now       = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    data = [
        {"range": f"{CREDS_TAB}!{_col_letter(repo_idx)}{sheet_row}",   "values": [[CURRENT_REPO]]},
        {"range": f"{CREDS_TAB}!{_col_letter(status_idx)}{sheet_row}", "values": [[ASSIGN_STATUS_IN_USE]]},
    ]
    if at_idx is not None:
        data.append({"range": f"{CREDS_TAB}!{_col_letter(at_idx)}{sheet_row}", "values": [[now]]})

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=MASTER_SHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


# ═══════════════════════════════════════════════════════════════════════════
#  ACCOUNT CONFIG + LIVE SETTINGS — from Sheet1, row ACCOUNT_ROW.
# ═══════════════════════════════════════════════════════════════════════════
#
#  Expected Sheet1 header (case-insensitive), per-account identity:
#
#  BSKY_HANDLE | BSKY_APP_PW | LINK_URL | LINK_DISPLAY_TEXT | HASHTAGS |
#  MEGA_UPLOAD_FOLDER | MEGA_PROCESSED_FOLDER |
#  LOCKED_BY | LOCKED_AT   (optional) |
#  ASSIGNED_REPO | ASSIGNED_STATUS | ASSIGNED_AT   (optional) |
#  POST_PLAN_SHEET_NAME  (optional — set a per-account post-plan tab name;
#                         falls back to the Settings tab value, then "Sheet1")
#
#  Any of the "live settings" below can ALSO be set per-row in Sheet1 (as an
#  extra column with the same name) to override the shared Settings tab
#  value for just that one account:
#
#  IMAGE_RATIO | VIDEO_RATIO | HASHTAGS_ENABLED_IMAGE | HASHTAGS_ENABLED_VIDEO |
#  LINK_ENABLED_IMAGE | LINK_ENABLED_VIDEO | LINK_PERCENTAGE | MAX_IMAGE_MB |
#  CAPTION_ENABLED | ENABLE_REPORT | REPORT_TIMES_PER_DAY | TOP_POSTS_COUNT |
#  TOP_POSTS_WITHIN | POST_PLAN_SHEET_NAME | LOOP_INTERVAL_SECONDS

CREDS_RANGE = f"{CREDS_TAB}!A:Z"

_account_config         = None
_creds_lock_col_by      = None
_creds_lock_col_at      = None
_global_settings_cache  = None

DEFAULT_LOOP_INTERVAL_SECONDS = 1800


def load_global_settings(force_refresh=False):
    global _global_settings_cache
    if _global_settings_cache is not None and not force_refresh:
        return _global_settings_cache

    settings = {}
    try:
        service = get_sheets_service()
        result  = service.spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range=f"{SETTINGS_TAB}!A:B"
        ).execute()
        values = result.get("values", [])
        for row in values[1:]:
            if len(row) >= 1 and row[0].strip():
                key = row[0].strip().upper()
                val = row[1].strip() if len(row) > 1 else ""
                settings[key] = val
    except Exception as exc:
        print(f"Note: '{SETTINGS_TAB}' tab not found or unreadable — using built-in "
              f"defaults for shared settings ({exc}).")

    _global_settings_cache = settings
    return _global_settings_cache


def load_account_config(force_refresh=False):
    global _account_config, _creds_lock_col_by, _creds_lock_col_at

    if _account_config is not None and not force_refresh:
        return _account_config

    service = get_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=MASTER_SHEET_ID, range=CREDS_RANGE
    ).execute()
    values = result.get("values", [])

    if len(values) < 2:
        raise RuntimeError(
            f"'{CREDS_TAB}' in the master sheet is empty or has only a header. "
            "Add at least one account data row."
        )

    data_idx = ACCOUNT_ROW
    if data_idx >= len(values):
        raise RuntimeError(
            f"ACCOUNT_ROW={ACCOUNT_ROW} but '{CREDS_TAB}' only has "
            f"{len(values)-1} data row(s)."
        )

    header = [h.strip().upper() for h in values[0]]
    row    = values[data_idx]

    def col(*names):
        for n in names:
            try:
                idx = header.index(n.upper())
                return row[idx].strip() if idx < len(row) else ""
            except ValueError:
                continue
        return ""

    _creds_lock_col_by = header.index("LOCKED_BY") if "LOCKED_BY" in header else None
    _creds_lock_col_at = header.index("LOCKED_AT") if "LOCKED_AT" in header else None

    shared = load_global_settings(force_refresh)
    def setting(key):
        return col(key) or shared.get(key, "")

    raw_link     = col("LINK_URL") or "https://foodiesposts.com"
    link_url     = raw_link if raw_link.startswith("http") else f"https://{raw_link}"
    link_display = col("LINK_DISPLAY_TEXT") or link_url.replace("https://","").replace("http://","")

    img_ratio_raw = _parse_pct(setting("IMAGE_RATIO"), 0.60)
    vid_ratio_raw = _parse_pct(setting("VIDEO_RATIO"), 0.40)
    ratio_sum     = img_ratio_raw + vid_ratio_raw
    image_ratio   = (img_ratio_raw / ratio_sum) if ratio_sum > 0 else 0.60
    video_ratio   = (vid_ratio_raw / ratio_sum) if ratio_sum > 0 else 0.40

    cfg = {
        "handle":                col("BSKY_HANDLE"),
        "app_pw":                col("BSKY_APP_PW"),
        "link_url":              link_url,
        "link_display_text":     link_display,
        "hashtags_raw":          col("HASHTAGS"),
        "mega_upload_folder":    col("MEGA_UPLOAD_FOLDER"),
        "mega_processed_folder": col("MEGA_PROCESSED_FOLDER"),
        "row_num":               ACCOUNT_ROW,

        "image_ratio":              image_ratio,
        "video_ratio":              video_ratio,
        "hashtags_enabled_image":   _parse_bool(setting("HASHTAGS_ENABLED_IMAGE"), True),
        "hashtags_enabled_video":   _parse_bool(setting("HASHTAGS_ENABLED_VIDEO"), False),
        "link_enabled_image":       _parse_bool(setting("LINK_ENABLED_IMAGE"), True),
        "link_enabled_video":       _parse_bool(setting("LINK_ENABLED_VIDEO"), True),
        "link_percentage":          _parse_pct(setting("LINK_PERCENTAGE"), 1.0),
        "max_image_bytes":          int(_parse_plain_float(setting("MAX_IMAGE_MB"), 2.0) * 1024 * 1024),
        "caption_enabled":          _parse_bool(setting("CAPTION_ENABLED"), True),
        "enable_report":            _parse_bool(setting("ENABLE_REPORT"), False),
        "report_times_per_day":     _parse_int(setting("REPORT_TIMES_PER_DAY"), 1),
        "top_posts_count":          _parse_int(setting("TOP_POSTS_COUNT"), 1),
        "top_posts_within":         _parse_int(setting("TOP_POSTS_WITHIN"), 30),
        "post_plan_sheet_name":     setting("POST_PLAN_SHEET_NAME") or "Sheet1",
        "loop_interval_seconds":    _parse_int(setting("LOOP_INTERVAL_SECONDS"),
                                                DEFAULT_LOOP_INTERVAL_SECONDS),

        "locked_by": col("LOCKED_BY"),
        "locked_at": col("LOCKED_AT"),
    }

    if not cfg["handle"]:
        raise RuntimeError(
            f"BSKY_HANDLE is empty for account row {ACCOUNT_ROW} in '{CREDS_TAB}'."
        )

    _account_config = cfg
    return cfg

def _cfg():
    return load_account_config()

def refresh_account_config():
    return load_account_config(force_refresh=True)


# ═══════════════════════════════════════════════════════════════════════════
#  CROSS-REPO SOFT LOCK (unchanged)
# ═══════════════════════════════════════════════════════════════════════════

class AccountLockedElsewhereError(Exception):
    """Non-fatal — another repo currently owns this account row."""


def _write_lock_heartbeat(owner, ts):
    try:
        service = get_sheets_service()
        by_col  = _col_letter(_creds_lock_col_by)
        at_col  = _col_letter(_creds_lock_col_at)
        sheet_row = ACCOUNT_ROW + 1
        service.spreadsheets().values().update(
            spreadsheetId=MASTER_SHEET_ID,
            range=f"{CREDS_TAB}!{by_col}{sheet_row}:{at_col}{sheet_row}",
            valueInputOption="RAW",
            body={"values": [[owner, ts]]},
        ).execute()
        if _account_config:
            _account_config["locked_by"] = owner
            _account_config["locked_at"] = ts
    except Exception as exc:
        print(f"Warning: could not write account lock heartbeat: {exc}")


def try_acquire_account_lock():
    cfg = refresh_account_config()

    if _creds_lock_col_by is None or _creds_lock_col_at is None:
        return True

    locked_by     = cfg.get("locked_by", "")
    locked_at_raw = cfg.get("locked_at", "")

    stale = True
    if locked_at_raw:
        try:
            locked_at = time.mktime(time.strptime(locked_at_raw, "%Y-%m-%dT%H:%M:%SZ"))
            stale = (time.time() - locked_at) > LOCK_TTL_MINUTES * 60
        except ValueError:
            stale = True

    if locked_by and locked_by != CURRENT_REPO and not stale:
        print(f"Row {ACCOUNT_ROW} is currently locked by '{locked_by}' "
              f"(last heartbeat {locked_at_raw} UTC, TTL {LOCK_TTL_MINUTES}m). Skipping this run.")
        return False

    _write_lock_heartbeat(CURRENT_REPO, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  TEXT HELPERS (unchanged)
# ═══════════════════════════════════════════════════════════════════════════

def _posting_handle():
    h = _cfg()["handle"]
    return h if h.startswith("@") else f"@{h}"

def replace_mentions(text):
    return _MENTION_RE.sub(_posting_handle(), text) if text else text

def replace_urls(text):
    return _URL_RE.sub(_cfg()["link_url"], text) if text else text


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

def print_config_summary():
    cfg = _cfg()
    print("── Run config (live from 'Settings' tab + Sheet1, re-checked every cycle) ──")
    print(f"  Account row:              {cfg['row_num']}  ({_posting_handle()})")
    print(f"  Mega upload folder:       {cfg['mega_upload_folder'] or '(not set!)'}")
    print(f"  Mega processed folder:    {cfg['mega_processed_folder'] or '(not set!)'}")
    print(f"  Post link:                {cfg['link_display_text']} -> {cfg['link_url']}")
    print(f"  Image ratio:              {cfg['image_ratio']:.0%}")
    print(f"  Video ratio:              {cfg['video_ratio']:.0%}")
    print(f"  Caption enabled:          {cfg['caption_enabled']}")
    print(f"  Hashtags on image posts:  {cfg['hashtags_enabled_image']}")
    print(f"  Hashtags on video posts:  {cfg['hashtags_enabled_video']}")
    print(f"  Link on image posts:      {cfg['link_enabled_image']}")
    print(f"  Link on video posts:      {cfg['link_enabled_video']}")
    print(f"  Link inclusion rate:      {cfg['link_percentage']:.0%} of eligible posts")
    print(f"  Max image size:           {cfg['max_image_bytes']/(1024*1024):.2f} MB")
    print(f"  Loop interval:            {cfg['loop_interval_seconds']}s ({cfg['loop_interval_seconds']/60:.1f} min)")
    print(f"  Generate report:          {cfg['enable_report']}")
    if cfg["enable_report"]:
        print(f"  Report frequency:         {cfg['report_times_per_day']}x per 24h")
        print(f"  Top posts combined:       {cfg['top_posts_count']}")
        print(f"  Scan last N posts:        {cfg['top_posts_within']}")
    print(f"  Post-plan tab:            {cfg['post_plan_sheet_name']}")
    print(f"  Google token source:      {'scraped from GOOGLE_TOKEN_URL' if GOOGLE_TOKEN_URL else 'GOOGLE_OAUTH_CREDENTIALS secret'}")
    if _creds_lock_col_by is not None:
        print(f"  Cross-repo lock:          enabled (owner={cfg.get('locked_by') or '—'}, "
              f"last heartbeat={cfg.get('locked_at') or '—'})")
    else:
        print("  Cross-repo lock:          disabled (add LOCKED_BY / LOCKED_AT columns to enable)")
    print("─────────────────────────────────────────────────")


# ═══════════════════════════════════════════════════════════════════════════
#  REPORT TAB — simplified: ONE row per check-in, plain-English columns,
#  frequency controlled by REPORT_TIMES_PER_DAY instead of a hardcoded
#  once-per-calendar-day check.
# ═══════════════════════════════════════════════════════════════════════════

def _now_str():
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime()) + " UTC"

def _parse_report_ts(s):
    try:
        return time.mktime(time.strptime(s.replace(" UTC", ""), "%Y-%m-%d %H:%M"))
    except Exception:
        return None


def _ensure_report_tab(service):
    try:
        meta     = service.spreadsheets().get(spreadsheetId=MASTER_SHEET_ID).execute()
        existing = {s["properties"]["title"].strip().lower()
                    for s in meta.get("sheets", [])}
        if REPORT_TAB.lower() not in existing:
            service.spreadsheets().batchUpdate(
                spreadsheetId=MASTER_SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": REPORT_TAB}}}]},
            ).execute()
            print(f"Created '{REPORT_TAB}' tab.")
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            print(f"Warning: could not verify/create Report tab: {exc}")

    last_col = _col_letter(len(REPORT_HEADER) - 1)
    try:
        r = service.spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A1:{last_col}1"
        ).execute()
        existing_header = r.get("values", [[]])[0] if r.get("values") else []
        if existing_header != REPORT_HEADER:
            service.spreadsheets().values().update(
                spreadsheetId=MASTER_SHEET_ID,
                range=f"{REPORT_TAB}!A1:{last_col}1",
                valueInputOption="RAW",
                body={"values": [REPORT_HEADER]},
            ).execute()
            print(f"Set '{REPORT_TAB}' header to: {REPORT_HEADER}")
    except Exception as exc:
        print(f"Warning: could not check/update report header: {exc}")


def _last_report_for_handle(service, handle):
    """Returns (timestamp_str, followers_int_or_None) for the most recent
    report row for this handle, or (None, None) if there isn't one yet."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=MASTER_SHEET_ID, range=f"{REPORT_TAB}!A:G"
        ).execute()
        rows = result.get("values", [])[1:]
        for row in reversed(rows):
            if len(row) >= 2 and row[1] == handle:
                ts = row[0] if len(row) > 0 else None
                followers = None
                if len(row) > 2 and row[2].strip():
                    try:
                        followers = int(row[2])
                    except ValueError:
                        followers = None
                return ts, followers
    except Exception:
        pass
    return None, None


def _report_due(service, handle, times_per_day):
    times_per_day = max(1, times_per_day)
    last_ts, _ = _last_report_for_handle(service, handle)
    if last_ts is None:
        return True
    last_epoch = _parse_report_ts(last_ts)
    if last_epoch is None:
        return True
    interval_seconds = 86400.0 / times_per_day
    return (time.time() - last_epoch) >= interval_seconds


def _append_report(service, rows):
    service.spreadsheets().values().append(
        spreadsheetId=MASTER_SHEET_ID,
        range=f"{REPORT_TAB}!A:G",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def _top_post_summary(client, handle, top_n, within):
    """Returns (summary_text, top_engagement_int). Combines up to top_n
    posts into one readable cell instead of writing extra rows."""
    try:
        response = client.get_author_feed(actor=handle, limit=within)
    except Exception as exc:
        return f"(couldn't fetch posts: {exc})", 0

    posts = []
    for item in response.feed:
        if getattr(item, "reason", None) is not None:
            continue
        p       = item.post
        likes   = getattr(p, "like_count",   0) or 0
        reposts = getattr(p, "repost_count", 0) or 0
        replies = getattr(p, "reply_count",  0) or 0
        quotes  = getattr(p, "quote_count",  0) or 0
        try:
            text = p.record.text or ""
        except AttributeError:
            text = ""
        posts.append({
            "text": text, "likes": likes, "reposts": reposts,
            "replies": replies, "quotes": quotes,
            "engagement": likes + reposts + replies + quotes,
        })

    if not posts:
        return "(no posts found)", 0

    ranked = sorted(posts, key=lambda p: p["engagement"], reverse=True)[:max(1, top_n)]

    if len(ranked) == 1:
        p = ranked[0]
        preview = p["text"][:100] + ("…" if len(p["text"]) > 100 else "")
        return preview, p["engagement"]

    parts = []
    for i, p in enumerate(ranked, start=1):
        preview = p["text"][:60] + ("…" if len(p["text"]) > 60 else "")
        parts.append(f"{i}) {preview} ({p['engagement']})")
    return " | ".join(parts), ranked[0]["engagement"]


def generate_report(client, handle, service, cfg):
    if not _report_due(service, handle, cfg["report_times_per_day"]):
        print(f"Report for {handle} not due yet (limit: {cfg['report_times_per_day']}x/24h).")
        return
    try:
        profile = client.get_profile(actor=handle)
        total   = profile.followers_count or 0

        _, prev_followers = _last_report_for_handle(service, handle)
        prev   = prev_followers if prev_followers is not None else total
        gained = total - prev

        top_preview, top_engagement = _top_post_summary(
            client, handle, cfg["top_posts_count"], cfg["top_posts_within"]
        )

        row = [_now_str(), handle, total, gained, top_preview, top_engagement, "OK"]
        _append_report(service, [row])
        print(f"Report logged for {handle}: {total} followers ({gained:+d} since last), "
              f"top post engagement={top_engagement}.")
    except Exception as exc:
        print(f"Warning: report generation failed: {exc}")


def run_report(client, handle, cfg):
    try:
        service = get_sheets_service()
        _ensure_report_tab(service)
        generate_report(client, handle, service, cfg)
    except Exception as exc:
        print(f"Warning: report generation failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
#  ERROR TYPES
# ═══════════════════════════════════════════════════════════════════════════

class AccountTakenDownError(Exception):
    """Fatal — log to sheet, disable workflow."""

class NoMediaFoundError(Exception):
    """Clean exit (code 0) — keep schedule running."""


def log_account_problem(handle, status):
    try:
        service = get_sheets_service()
        _ensure_report_tab(service)
        _append_report(service, [[_now_str(), handle, "", "", "", "", status]])
        print(f"Logged '{status}' for {handle}.")
    except Exception as exc:
        print(f"Warning: could not log account status: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
#  ACCOUNT DISPLAY
# ═══════════════════════════════════════════════════════════════════════════

def print_target_account(handle):
    display = handle if handle.startswith("@") else f"@{handle}"
    print(f"Target Bluesky account: {display}")
    print(f"  (app password: {'loaded' if _cfg().get('app_pw') else 'MISSING!'})")


# ═══════════════════════════════════════════════════════════════════════════
#  HASHTAGS
# ═══════════════════════════════════════════════════════════════════════════

def get_account_hashtags():
    raw = _cfg().get("hashtags_raw", "")
    if raw:
        tags = [w.lstrip("#") for w in raw.split() if w.startswith("#")]
        if tags:
            return tags
    try:
        with open("hashtags.txt", "r", encoding="utf-8") as f:
            sets = [l.strip() for l in f if l.strip()]
        return [w.lstrip("#") for w in random.choice(sets).split() if w.startswith("#")] if sets else []
    except FileNotFoundError:
        return []


# ═══════════════════════════════════════════════════════════════════════════
#  LINK-IN-POST DECISION
# ═══════════════════════════════════════════════════════════════════════════

def should_add_link(kind):
    cfg     = _cfg()
    enabled = cfg["link_enabled_image"] if kind == "image" else cfg["link_enabled_video"]
    if not enabled:
        return False
    return random.random() < cfg["link_percentage"]


# ═══════════════════════════════════════════════════════════════════════════
#  POST-PLAN SHEET (unchanged — still File Name + Caption + Status)
# ═══════════════════════════════════════════════════════════════════════════

_post_plan_cache          = None
_post_plan_status_col_idx = None


def get_post_plan_tab_name():
    return _cfg()["post_plan_sheet_name"]


def load_post_plan(force_refresh=False):
    global _post_plan_cache, _post_plan_status_col_idx
    if _post_plan_cache is not None and not force_refresh:
        return _post_plan_cache

    tab     = get_post_plan_tab_name()
    service = get_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=POST_PLAN_SHEET_ID, range=f"{tab}!A:Z"
    ).execute()
    values  = result.get("values", [])
    if not values:
        print(f"Warning: post-plan tab '{tab}' is empty.")
        _post_plan_cache = {}
        return _post_plan_cache

    header = [h.strip().lower() for h in values[0]]
    def ci(*names):
        for n in names:
            if n in header: return header.index(n)
        return None

    file_idx    = ci("file name", "filename", "file")
    caption_idx = ci("caption", "captions")
    status_idx  = ci("status")
    _post_plan_status_col_idx = status_idx

    if file_idx is None or caption_idx is None:
        print(f"Warning: post-plan needs 'File Name' and 'Caption' columns. Found: {header}")
        _post_plan_cache = {}
        return _post_plan_cache
    if status_idx is None:
        print("Warning: no 'Status' column — posted files won't be tracked.")

    plan_exact = {}
    plan_lower = {}
    already    = 0
    for i, row in enumerate(values[1:], start=2):
        fname   = row[file_idx].strip()    if len(row) > file_idx    else ""
        caption = row[caption_idx].strip() if len(row) > caption_idx else ""
        status  = row[status_idx].strip()  if status_idx is not None and len(row) > status_idx else ""
        if not fname: continue
        entry = {"caption": caption, "row": i, "status": status}
        plan_exact[fname]         = entry
        plan_lower[fname.lower()] = entry
        if status.lower() == POSTED_STATUS_VALUE: already += 1

    print(f"Loaded {len(plan_exact)} post-plan rows ({already} already posted).")
    _post_plan_cache = {"exact": plan_exact, "lower": plan_lower}
    return _post_plan_cache


def find_plan_entry(plan, filename):
    exact = plan.get("exact", {})
    lower = plan.get("lower", {})
    return (
        exact.get(filename)
        or lower.get(filename.lower())
        or lower.get(os.path.splitext(filename.lower())[0])
    )


def mark_posted(filename, row_number, retries=3):
    global _post_plan_cache
    if _post_plan_status_col_idx is None:
        print(f"Warning: no 'Status' column — cannot mark '{filename}' as posted.")
        return
    for attempt in range(1, retries + 1):
        try:
            tab     = get_post_plan_tab_name()
            col_l   = _col_letter(_post_plan_status_col_idx)
            service = get_sheets_service()
            service.spreadsheets().values().update(
                spreadsheetId=POST_PLAN_SHEET_ID,
                range=f"{tab}!{col_l}{row_number}",
                valueInputOption="RAW",
                body={"values": [[POSTED_STATUS_VALUE]]},
            ).execute()
            if _post_plan_cache:
                for d in (_post_plan_cache.get("exact",{}), _post_plan_cache.get("lower",{})):
                    if filename in d: d[filename]["status"] = POSTED_STATUS_VALUE
                    if filename.lower() in d: d[filename.lower()]["status"] = POSTED_STATUS_VALUE
            print(f"Marked '{filename}' row {row_number} as posted.")
            return
        except Exception as exc:
            if attempt < retries:
                wait = 2 ** attempt
                print(f"  mark_posted attempt {attempt}/{retries} failed ({exc}); retrying in {wait}s…")
                time.sleep(wait)
            else:
                print(f"ERROR: could not mark '{filename}' as posted after {retries} attempts: {exc}")
                print("  Post was successful — file will be moved. Row may need manual update.")


# ═══════════════════════════════════════════════════════════════════════════
#  MEGA.NZ HELPERS (via rclone) — replaces the old Google Drive section.
#  Same claim/download/post/move-to-processed flow as before, just backed
#  by rclone commands against the Mega remote instead of the Drive API.
# ═══════════════════════════════════════════════════════════════════════════

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".avif", ".heic"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".3gp", ".ts"}


def _kind_from_filename(filename):
    ext = os.path.splitext(filename.lower())[1]
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    return None


def choose_media_kind():
    cfg = _cfg()
    return random.choices(["image", "video"], weights=[cfg["image_ratio"], cfg["video_ratio"]], k=1)[0]


def _rclone_run(args):
    return subprocess.run(["rclone", "--config", RCLONE_CONFIG_PATH] + args,
                           capture_output=True, text=True)


def rclone_list_files(remote_folder):
    result = _rclone_run(["lsf", remote_folder, "--files-only"])
    if result.returncode != 0:
        print(f"Warning: rclone lsf failed for '{remote_folder}': {result.stderr.strip()[-300:]}")
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def rclone_claim(remote_folder, name):
    """Server-side rename to claim a file. If another runner already
    claimed/moved it, moveto fails harmlessly and we just try the next
    candidate — this is the Mega equivalent of the old Drive claim-rename."""
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{name}"
    result = _rclone_run(["moveto", f"{remote_folder}/{name}", f"{remote_folder}/{claimed_name}"])
    return claimed_name if result.returncode == 0 else None


def rclone_download(remote_folder, filename, local_path):
    result = _rclone_run(["copyto", f"{remote_folder}/{filename}", local_path])
    return result.returncode == 0


def rclone_move(src, dst):
    result = _rclone_run(["moveto", src, dst])
    return result.returncode == 0


def fetch_media_matching_plan(preferred_kind, plan):
    cfg           = _cfg()
    upload_folder = cfg["mega_upload_folder"]
    if not upload_folder:
        raise RuntimeError("MEGA_UPLOAD_FOLDER is empty in credentials sheet.")

    remote_folder = f"{RCLONE_REMOTE_NAME}:{upload_folder}"
    files = rclone_list_files(remote_folder)
    candidates = [f for f in files if not f.startswith(CLAIM_PREFIX)
                  and _kind_from_filename(f) == preferred_kind]

    counters = {"claim": 0, "plan": 0, "posted": 0}

    for name in candidates:
        entry = find_plan_entry(plan, name)
        if entry is None:
            counters["plan"] += 1
            continue
        if entry["status"].lower() == POSTED_STATUS_VALUE:
            counters["posted"] += 1
            continue

        print(f"Found {preferred_kind}: '{name}'")
        claimed_name = rclone_claim(remote_folder, name)
        if claimed_name is None:
            counters["claim"] += 1
            continue
        print(f"Claimed as '{claimed_name}'.")

        local_path = f"/tmp/{name}"
        if not rclone_download(remote_folder, claimed_name, local_path):
            print(f"Warning: download failed for claimed file '{claimed_name}' — releasing claim.")
            rclone_move(f"{remote_folder}/{claimed_name}", f"{remote_folder}/{name}")
            continue

        file_info = {"original_name": name, "claimed_name": claimed_name}
        return file_info, local_path, preferred_kind, entry["caption"], entry["row"]

    print(f"No {preferred_kind} match: "
          f"{counters['plan']} not in plan, {counters['posted']} already posted, "
          f"{counters['claim']} claimed by another run.")
    return None, None, None, None, None


def compress_image_under_limit(local_path):
    from PIL import Image
    max_bytes = _cfg()["max_image_bytes"]
    orig = os.path.getsize(local_path)
    if orig <= max_bytes:
        print(f"Image {orig/1024:.0f} KB — no compression needed.")
        return local_path
    img = Image.open(local_path)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    for q in range(90, 20, -10):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        if buf.tell() <= max_bytes:
            with open(local_path, "wb") as f: f.write(buf.getvalue())
            print(f"Compressed {orig/1024:.0f} KB → {buf.tell()/1024:.0f} KB (q={q}).")
            return local_path
    w, h = img.size
    scale = 0.9
    while scale > 0.3:
        r = img.resize((max(1,int(w*scale)), max(1,int(h*scale))), Image.LANCZOS)
        buf = io.BytesIO()
        r.save(buf, format="JPEG", quality=70, optimize=True)
        if buf.tell() <= max_bytes:
            with open(local_path, "wb") as f: f.write(buf.getvalue())
            print(f"Resized+compressed → {buf.tell()/1024:.0f} KB.")
            return local_path
        scale -= 0.1
    with open(local_path, "wb") as f: f.write(buf.getvalue())
    print(f"Warning: best-effort compression = {buf.tell()/1024:.0f} KB.")
    return local_path


def move_file_to_processed(claimed_name, original_name):
    cfg = _cfg()
    remote_upload    = f"{RCLONE_REMOTE_NAME}:{cfg['mega_upload_folder']}"
    remote_processed = f"{RCLONE_REMOTE_NAME}:{cfg['mega_processed_folder']}"
    ok = rclone_move(f"{remote_upload}/{claimed_name}", f"{remote_processed}/{original_name}")
    print("Moved to processed folder on Mega." if ok else
          "Warning: failed to move file to processed folder on Mega — check manually.")
    return ok


def release_claim(claimed_name, original_name):
    cfg = _cfg()
    remote_upload = f"{RCLONE_REMOTE_NAME}:{cfg['mega_upload_folder']}"
    ok = rclone_move(f"{remote_upload}/{claimed_name}", f"{remote_upload}/{original_name}")
    print(f"Released claim on '{original_name}'." if ok else
          f"Warning: could not release claim for '{original_name}'.")
    return ok


# ═══════════════════════════════════════════════════════════════════════════
#  POST BUILDING
# ═══════════════════════════════════════════════════════════════════════════

MAX_POST_GRAPHEMES = 300

def build_post_from_caption(caption, tags, add_link):
    cfg  = _cfg()
    text = replace_mentions(caption) if caption else ""

    def _assemble(caption_text):
        tb = TextBuilder()
        if add_link:
            m = _URL_RE.search(caption_text)
            if m:
                before = caption_text[:m.start()].rstrip()
                after  = _URL_RE.sub("", caption_text[m.end():]).strip()
                if before:
                    tb.text(before + " ")
                tb.link(cfg["link_display_text"], cfg["link_url"])
                if after:
                    tb.text(" " + after)
            else:
                if caption_text:
                    tb.text(caption_text)
                    tb.text("\n\n")
                tb.link(cfg["link_display_text"], cfg["link_url"])
        else:
            text_no_url = _URL_RE.sub("", caption_text).strip()
            if text_no_url:
                tb.text(text_no_url)

        if tags:
            tb.text("\n\n")
            for i, tag in enumerate(tags):
                tb.tag(f"#{tag}", tag)
                if i < len(tags) - 1:
                    tb.text(" ")
        return tb

    tb    = _assemble(text)
    plain = tb.build_text()

    if len(plain) > MAX_POST_GRAPHEMES:
        lo, hi, best_text = 0, len(text), ""
        while lo <= hi:
            mid   = (lo + hi) // 2
            trial = text[:mid].rstrip()
            if mid < len(text):
                trial += "…"
            if len(_assemble(trial).build_text()) <= MAX_POST_GRAPHEMES:
                best_text = trial
                lo = mid + 1
            else:
                hi = mid - 1
        print(f"Caption too long for post limit ({len(plain)} > {MAX_POST_GRAPHEMES}); "
              f"trimmed caption to fit.")
        tb = _assemble(best_text)

    return tb


def post_to_bluesky(client, media_name, local_path, kind, caption, tags, add_link):
    tb = build_post_from_caption(caption, tags, add_link)
    if kind == "video":
        with open(local_path, "rb") as f:
            client.send_video(text=tb, video=f.read(), video_alt=media_name)
    else:
        with open(local_path, "rb") as f:
            client.send_image(text=tb, image=f.read(), image_alt=media_name)

    preview = replace_mentions(caption or "")
    if add_link:
        m = _URL_RE.search(preview)
        if m:
            preview = (preview[:m.start()].rstrip()
                       + f" [{_cfg()['link_display_text']}]"
                       + _URL_RE.sub("", preview[m.end():]).strip())
        else:
            preview = (preview + f" [{_cfg()['link_display_text']}]").strip()
    else:
        preview = _URL_RE.sub("", preview).strip()
    print(f"✓ Posted {kind}: {preview!r} (link={'yes' if add_link else 'no'})")
    if tags:
        print(f"  Tags: {' '.join('#'+t for t in tags)}")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN CYCLE
# ═══════════════════════════════════════════════════════════════════════════

def run_once():
    cfg = refresh_account_config()

    if not try_acquire_account_lock():
        raise AccountLockedElsewhereError(
            f"Account row {ACCOUNT_ROW} is locked by another repo right now."
        )

    handle = cfg["handle"]

    print_target_account(handle)
    client = Client()
    try:
        client.login(handle, cfg["app_pw"])
    except Exception as exc:
        err = str(exc)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            raise AccountTakenDownError(f"Account {handle} taken down/suspended.") from exc
        if "AuthenticationRequired" in err or "Invalid identifier or password" in err:
            raise AccountTakenDownError(
                f"Auth failed for {handle} — check BSKY_HANDLE / BSKY_APP_PW in sheet row {ACCOUNT_ROW}."
            ) from exc
        raise

    if cfg["enable_report"]:
        run_report(client, handle, cfg)

    plan = load_post_plan()
    if not plan:
        raise NoMediaFoundError("Post-plan sheet has no usable rows.")

    preferred = choose_media_kind()
    fallback  = "video" if preferred == "image" else "image"

    file, path, kind, caption, row_num = fetch_media_matching_plan(preferred, plan)
    if not file:
        print(f"No {preferred} matched; trying {fallback}.")
        file, path, kind, caption, row_num = fetch_media_matching_plan(fallback, plan)

    if not file:
        raise NoMediaFoundError("No unposted Mega file matching the post-plan sheet.")

    original_name = file["original_name"]
    claimed_name  = file["claimed_name"]

    try:
        if kind == "image":
            path = compress_image_under_limit(path)

        cfg = _cfg()  # re-fetch in case the sheet changed between fetch and post
        hashtags_on = cfg["hashtags_enabled_image"] if kind == "image" else cfg["hashtags_enabled_video"]
        tags = get_account_hashtags() if hashtags_on else []
        add_link = should_add_link(kind)
        caption_to_use = caption if cfg.get("caption_enabled", True) else ""

        post_to_bluesky(client, original_name, path, kind, caption_to_use, tags, add_link)

    except Exception as exc:
        err = str(exc)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            release_claim(claimed_name, original_name)
            raise AccountTakenDownError(f"Account {handle} taken down mid-cycle.") from exc
        release_claim(claimed_name, original_name)
        print(f"Post failed — claim released, file stays in upload folder.")
        raise

    mark_posted(original_name, row_num)
    move_file_to_processed(claimed_name, original_name)
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    global ACCOUNT_ROW
    try:
        ACCOUNT_ROW = resolve_account_row()
        load_account_config()
    except Exception as exc:
        print(f"\n{'='*60}\nFATAL: {exc}\n{'='*60}\n")
        sys.exit(1)

    print_config_summary()
    print(f"Starting loop. Loop interval is read from the Settings tab "
          f"(LOOP_INTERVAL_SECONDS) and re-checked at the start of every cycle "
          f"— edit it in Google Sheets any time, no redeploy needed.")

    while True:
        cycle_start = time.time()
        try:
            run_once()
        except AccountLockedElsewhereError as exc:
            print(f"\n{'='*60}\n{exc}\nSkipping — schedule keeps running.\n{'='*60}\n")
            sys.exit(0)
        except NoMediaFoundError as exc:
            print(f"\n{'='*60}\nNO MEDIA: {exc}\nStopping — schedule keeps running.\n{'='*60}\n")
            sys.exit(0)
        except AccountTakenDownError as exc:
            handle  = (_account_config or {}).get("handle", "unknown")
            err_str = str(exc)
            reason  = ("🔑 AUTH FAILED — check handle/app-password in sheet"
                       if "Auth failed" in err_str or "app password" in err_str
                       else "⛔ ACCOUNT TAKEN DOWN / BANNED")
            print(f"\n{'='*60}\n{err_str}\n→ {reason}\n{'='*60}\n")
            log_account_problem(handle, status=reason)
            sys.exit(1)
        except Exception as exc:
            print(f"Error during cycle: {exc}")

        loop_interval = (_account_config or {}).get("loop_interval_seconds", DEFAULT_LOOP_INTERVAL_SECONDS)
        elapsed   = time.time() - cycle_start
        sleep_for = max(0, loop_interval - elapsed)
        print(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.1f}s "
              f"(interval={loop_interval}s from Settings tab)…")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
