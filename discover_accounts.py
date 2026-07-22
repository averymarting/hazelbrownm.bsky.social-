"""
Runs once at the start of the workflow (in the 'discover-accounts' job).

Reads the Credentials tab, filters out empty/banned/suspended rows, applies
an optional cap (MAX_ACCOUNTS_PER_RUN — from the workflow_dispatch input, or
the Settings tab, or unlimited if neither is set), and writes a JSON matrix
to $GITHUB_OUTPUT so the 'post' job can fan out one job per account row —
all within the same workflow run.

Env vars (all provided by the workflow, ultimately from repo/org secrets):
  GOOGLE_SHEET_ID               required — master spreadsheet ID
  GOOGLE_APPLICATION_CREDENTIALS  required — path to the service-account
                                   JSON file written by the workflow
  FORCE_ACCOUNT_ROW              optional — pins the run to exactly this
                                   row, skipping eligibility/limit checks
                                   (mirrors the old workflow_dispatch
                                   'account_row' override)
  MAX_ACCOUNTS_PER_RUN           optional — overrides the Settings-tab
                                   value of the same name for this run
"""
import json
import os
import sys

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

CREDS_TAB    = "Credentials"
SETTINGS_TAB = "Settings"

# Any ACCOUNT_STATUS value containing one of these (case-insensitive) is
# excluded from this and future runs — matches the status strings
# postnow_status_post.py writes on a fatal account error.
SKIP_STATUS_MARKERS = ("banned", "suspended", "taken down", "auth failed")


def get_env(name, required=True, default=""):
    v = os.getenv(name)
    if v is None or not v.strip():
        if required:
            raise RuntimeError(f"Missing required env var: {name}")
        return default
    return v.strip()


def main():
    sheet_id   = get_env("GOOGLE_SHEET_ID")
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "google_creds.json")
    if not os.path.exists(creds_path):
        raise RuntimeError(f"Google credentials file not found at {creds_path}")

    creds   = Credentials.from_service_account_file(creds_path, scopes=SHEETS_SCOPES)
    service = build("sheets", "v4", credentials=creds)

    values = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{CREDS_TAB}!A:Z"
    ).execute().get("values", [])

    if len(values) < 2:
        print(f"::error::'{CREDS_TAB}' has no data rows — add at least one account.")
        sys.exit(1)

    header = [h.strip().upper() for h in values[0]]

    def hidx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    handle_idx = hidx("BSKY_HANDLE")
    status_idx = hidx("ACCOUNT_STATUS")

    if handle_idx is None:
        print(f"::error::'{CREDS_TAB}' needs a 'BSKY_HANDLE' column.")
        sys.exit(1)

    force_row = get_env("FORCE_ACCOUNT_ROW", required=False)
    if force_row:
        try:
            eligible = [max(1, int(force_row))]
        except ValueError:
            print(f"::error::FORCE_ACCOUNT_ROW={force_row!r} is not a valid row number.")
            sys.exit(1)
        print(f"FORCE_ACCOUNT_ROW set — running only row {eligible[0]} "
              f"(eligibility filter and MAX_ACCOUNTS_PER_RUN cap skipped).")
    else:
        eligible = []
        for i, row in enumerate(values[1:], start=1):
            handle = row[handle_idx].strip() if len(row) > handle_idx else ""
            if not handle:
                continue
            status = (row[status_idx].strip().lower()
                      if status_idx is not None and len(row) > status_idx else "")
            if any(marker in status for marker in SKIP_STATUS_MARKERS):
                print(f"Skipping row {i} ({handle}) — status: {status!r}")
                continue
            eligible.append(i)

        if not eligible:
            print(f"::error::No eligible account rows in '{CREDS_TAB}' "
                  f"(all rows are empty or flagged banned/suspended).")
            sys.exit(1)

        # Cap: workflow_dispatch input takes priority; otherwise fall back
        # to the Settings tab's MAX_ACCOUNTS_PER_RUN; blank/unset -> run all.
        limit_raw = get_env("MAX_ACCOUNTS_PER_RUN", required=False)
        if not limit_raw:
            try:
                settings_values = service.spreadsheets().values().get(
                    spreadsheetId=sheet_id, range=f"{SETTINGS_TAB}!A:B"
                ).execute().get("values", [])
                settings = {
                    r[0].strip().upper(): (r[1].strip() if len(r) > 1 else "")
                    for r in settings_values[1:] if r and r[0].strip()
                }
                limit_raw = settings.get("MAX_ACCOUNTS_PER_RUN", "")
            except Exception as exc:
                print(f"Note: could not read '{SETTINGS_TAB}' tab for "
                      f"MAX_ACCOUNTS_PER_RUN ({exc}); running all eligible rows.")

        if limit_raw:
            try:
                limit = max(1, int(limit_raw))
                if limit < len(eligible):
                    print(f"Capping this run to the first {limit} of "
                          f"{len(eligible)} eligible accounts (MAX_ACCOUNTS_PER_RUN={limit}).")
                eligible = eligible[:limit]
            except ValueError:
                print(f"Warning: MAX_ACCOUNTS_PER_RUN={limit_raw!r} is not a valid "
                      f"number — running all eligible rows.")
        else:
            print("MAX_ACCOUNTS_PER_RUN not set — running all eligible accounts.")

    print(f"Account rows for this workflow run: {eligible}")
    matrix = {"account_row": eligible}

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        raise RuntimeError("GITHUB_OUTPUT is not set — must be run inside a GitHub Actions step.")
    with open(gh_output, "a") as f:
        f.write(f"matrix={json.dumps(matrix)}\n")
        f.write(f"count={len(eligible)}\n")


if __name__ == "__main__":
    main()
