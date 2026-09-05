"""Delivery: append to a Google Sheet, and email a digest of the top matches."""

import os
import json
import time
import smtplib
import logging
from email.message import EmailMessage
from datetime import date

log = logging.getLogger(__name__)

HEADERS = ["date", "score", "title", "company", "location", "source", "reason", "url", "status"]


# --------------------------------------------------------------------------
# Google Sheet
# --------------------------------------------------------------------------
def push_to_sheet(jobs):
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.getenv("SHEET_ID")
    if not (creds_json and sheet_id):
        log.warning("Sheet credentials missing, skipping sheet push")
        return

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    ws = gspread.authorize(creds).open_by_key(sheet_id).sheet1

    if not ws.get_all_values():
        ws.append_row(HEADERS)

    today = date.today().isoformat()
    rows = [
        [today, j["score"], j["title"], j["company"], j["location"],
         j["source"], j.get("reason", ""), j["url"], ""]
        for j in jobs
    ]
    for i in range(0, len(rows), 100):
        ws.append_rows(rows[i:i + 100], value_input_option="RAW")
        time.sleep(1)

    log.info("Pushed %d rows to sheet", len(rows))


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def _html(jobs, sheet_id):
    if not jobs:
        return "<p>No new matches today.</p>"

    rows = "".join(
        f"""<tr>
          <td style="padding:8px;font-weight:600;color:{'#1a7f37' if j['score'] >= 8 else '#57606a'}">{j['score']}</td>
          <td style="padding:8px"><a href="{j['url']}" style="color:#0969da;text-decoration:none">{j['title']}</a><br>
              <span style="color:#57606a;font-size:13px">{j['company']} &middot; {j['location']}</span></td>
          <td style="padding:8px;color:#57606a;font-size:13px">{j.get('reason','')}</td>
        </tr>"""
        for j in jobs
    )
    link = (
        f'<p style="font-size:13px"><a href="https://docs.google.com/spreadsheets/d/{sheet_id}">'
        "Open the full tracker sheet</a></p>" if sheet_id else ""
    )
    return f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:720px">
      <h2 style="margin-bottom:4px">{len(jobs)} new matches</h2>
      <p style="color:#57606a;margin-top:0;font-size:13px">{date.today().isoformat()}</p>
      <table style="border-collapse:collapse;width:100%">
        <tr style="text-align:left;border-bottom:2px solid #d0d7de">
          <th style="padding:8px">Fit</th><th style="padding:8px">Role</th><th style="padding:8px">Why</th>
        </tr>{rows}
      </table>{link}
    </div>"""


def send_email(jobs, top_n=15):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user, password = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    to = os.getenv("EMAIL_TO") or user
    if not (user and password and to):
        log.warning("SMTP credentials missing, skipping email")
        return

    top = jobs[:top_n]
    msg = EmailMessage()
    msg["Subject"] = f"Job radar: {len(top)} matches ({date.today().isoformat()})"
    msg["From"], msg["To"] = user, to
    msg.set_content("\n".join(f"[{j['score']}] {j['title']} @ {j['company']}\n{j['url']}" for j in top)
                    or "No new matches today.")
    msg.add_alternative(_html(top, os.getenv("SHEET_ID")), subtype="html")

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    log.info("Emailed %d jobs to %s", len(top), to)
