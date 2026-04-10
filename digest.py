import os
import base64
import json
import imaplib
import smtplib
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import anthropic
import pytz

# ── Config from environment variables ──────────────────────────────────────
GMAIL_ADDRESS   = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASS  = os.environ["GMAIL_APP_PASSWORD"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
SEND_TO         = os.environ.get("SEND_TO", GMAIL_ADDRESS)
BUCHAREST_TZ    = pytz.timezone("Europe/Bucharest")

# ── Step 1: Fetch the latest PDF attachment from Gmail ─────────────────────
def fetch_latest_pdf(days_back=0):
    """Connect to Gmail via IMAP and return raw PDF bytes for the report `days_back` days ago."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
    mail.select("inbox")

    target_date = datetime.now(BUCHAREST_TZ) - timedelta(days=days_back)
    # Search for the report email
    search_date = target_date.strftime("%d-%b-%Y")
    status, data = mail.search(
        None,
        f'(FROM "vnrroinfo@veryniceretail.com" SUBJECT "VNR-ROU Sales on group by days" ON {search_date})'
    )

    if status != "OK" or not data[0]:
        # Try a broader search (last 3 days) if exact date fails
        status, data = mail.search(
            None,
            '(FROM "vnrroinfo@veryniceretail.com" SUBJECT "VNR-ROU Sales on group by days")'
        )
        if status != "OK" or not data[0]:
            raise Exception(f"No report email found for {search_date}")

    # Get the most recent matching email
    msg_ids = data[0].split()
    if days_back == 0:
        msg_id = msg_ids[-1]   # most recent
    elif days_back == 1:
        msg_id = msg_ids[-2] if len(msg_ids) >= 2 else msg_ids[-1]
    else:
        idx = min(days_back, len(msg_ids) - 1)
        msg_id = msg_ids[-(idx + 1)]

    status, msg_data = mail.fetch(msg_id, "(RFC822)")
    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)
    mail.logout()

    # Extract PDF attachment
    for part in msg.walk():
        if part.get_content_type() == "application/pdf":
            return part.get_payload(decode=True)

    raise Exception("No PDF attachment found in the email")


# ── Step 2: Extract structured sales data from PDF using Claude ────────────
def extract_sales_data(pdf_bytes, report_label):
    """Send PDF to Claude and get back structured JSON sales data."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=(
            "You are a sales data extraction assistant for VNR Romania retail reports. "
            "Extract ALL data precisely. Return ONLY valid JSON — no explanation, no markdown fences."
        ),
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64
                    }
                },
                {
                    "type": "text",
                    "text": (
                        f"This is the VNR-ROU Sales report: {report_label}.\n\n"
                        "Extract ALL sales data. Return ONLY this JSON structure:\n"
                        "{\n"
                        '  "report_date": "YYYY-MM-DD",\n'
                        '  "days_in_report": <int>,\n'
                        '  "locations": [\n'
                        '    {\n'
                        '      "name": "location name",\n'
                        '      "categories": [\n'
                        '        { "name": "category name", "sales": <float> }\n'
                        '      ],\n'
                        '      "total": <float>\n'
                        '    }\n'
                        '  ],\n'
                        '  "grand_total": <float>\n'
                        "}"
                    )
                }
            ]
        }]
    )

    raw = response.content[0].text
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


# ── Step 3: Build the HTML digest email ───────────────────────────────────
def pct_change(current, previous):
    if not previous:
        return 0.0
    return ((current - previous) / previous) * 100

def fmt_ron(n):
    return f"{n:,.0f}".replace(",", ".") + " RON"

def fmt_pct(n):
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.1f}%"

def arrow(n):
    return "↑" if n >= 0 else "↓"

def color(n):
    return "#4ade80" if n >= 0 else "#f87171"

def build_email_html(today_data, prev_data, today_label, prev_label):
    total_change = pct_change(today_data["grand_total"], prev_data["grand_total"])

    # Find best/worst location
    loc_changes = []
    for loc in today_data["locations"]:
        prev_loc = next((l for l in prev_data["locations"] if l["name"] == loc["name"]), None)
        if prev_loc:
            chg = pct_change(loc["total"], prev_loc["total"])
            loc_changes.append((loc["name"], chg, loc["total"]))
    loc_changes.sort(key=lambda x: x[1], reverse=True)
    best_loc = loc_changes[0] if loc_changes else None
    worst_loc = loc_changes[-1] if loc_changes else None

    # Category totals
    cat_totals = {}
    prev_cat_totals = {}
    for loc in today_data["locations"]:
        for cat in loc["categories"]:
            cat_totals[cat["name"]] = cat_totals.get(cat["name"], 0) + cat["sales"]
    for loc in prev_data["locations"]:
        for cat in loc["categories"]:
            prev_cat_totals[cat["name"]] = prev_cat_totals.get(cat["name"], 0) + cat["sales"]

    cat_changes = [(n, pct_change(v, prev_cat_totals.get(n, v))) for n, v in cat_totals.items()]
    cat_changes.sort(key=lambda x: x[1], reverse=True)
    best_cat = cat_changes[0] if cat_changes else None
    worst_cat = cat_changes[-1] if cat_changes else None

    now_str = datetime.now(BUCHAREST_TZ).strftime("%A, %d %B %Y")

    # Build location rows HTML
    loc_rows_html = ""
    for loc in today_data["locations"]:
        prev_loc = next((l for l in prev_data["locations"] if l["name"] == loc["name"]), None)
        if not prev_loc:
            continue
        loc_chg = pct_change(loc["total"], prev_loc["total"])
        loc_color = color(loc_chg)

        cat_rows = ""
        for cat in loc["categories"]:
            prev_cat = next((c for c in prev_loc["categories"] if c["name"] == cat["name"]), None)
            prev_val = prev_cat["sales"] if prev_cat else 0
            cat_chg = pct_change(cat["sales"], prev_val)
            cat_color = color(cat_chg)
            cat_rows += f"""
            <tr>
              <td style="padding:8px 12px;font-size:13px;color:#cccccc;border-bottom:1px solid #2a2a2a">{cat["name"]}</td>
              <td style="padding:8px 12px;font-size:13px;color:#f0f0f0;text-align:right;border-bottom:1px solid #2a2a2a;font-family:monospace">{fmt_ron(cat["sales"])}</td>
              <td style="padding:8px 12px;font-size:13px;color:#666;text-align:right;border-bottom:1px solid #2a2a2a;font-family:monospace">{fmt_ron(prev_val)}</td>
              <td style="padding:8px 12px;font-size:13px;color:{cat_color};text-align:right;border-bottom:1px solid #2a2a2a;font-weight:600;font-family:monospace">{arrow(cat_chg)} {fmt_pct(cat_chg)}</td>
            </tr>"""

        loc_rows_html += f"""
        <tr>
          <td colspan="4" style="padding:0">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#1a1a1a;border-radius:8px;margin-bottom:8px;overflow:hidden">
              <tr style="background:#222">
                <td style="padding:12px 14px;font-size:14px;font-weight:600;color:#f0f0f0">{loc["name"]}</td>
                <td style="padding:12px 14px;font-size:13px;color:#f0f0f0;text-align:right;font-family:monospace">{fmt_ron(loc["total"])}</td>
                <td style="padding:12px 14px;font-size:13px;color:#666;text-align:right;font-family:monospace">{fmt_ron(prev_loc["total"])}</td>
                <td style="padding:12px 14px;font-size:14px;font-weight:700;color:{color(loc_chg)};text-align:right;font-family:monospace">{arrow(loc_chg)} {fmt_pct(loc_chg)}</td>
              </tr>
              {cat_rows}
            </table>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:'Helvetica Neue',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f0f0f;padding:32px 16px">
  <tr><td align="center">
    <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%">

      <!-- Header -->
      <tr>
        <td style="padding-bottom:24px">
          <p style="margin:0;font-size:11px;color:#444;letter-spacing:0.1em;text-transform:uppercase;font-family:monospace">VNR · ROU Sales Intelligence</p>
          <h1 style="margin:4px 0 0;font-size:26px;font-weight:300;color:#f0f0f0;letter-spacing:-0.5px">Morning <span style="color:#4ade80;font-weight:600">Sales Digest</span></h1>
          <p style="margin:4px 0 0;font-size:13px;color:#555">{now_str} &nbsp;·&nbsp; {today_label} vs {prev_label}</p>
        </td>
      </tr>

      <!-- Highlight banner -->
      <tr>
        <td style="background:#111;border-left:3px solid {color(total_change)};border-radius:0 8px 8px 0;padding:14px 16px;margin-bottom:20px">
          <p style="margin:0;font-size:14px;color:#aaa;line-height:1.7">
            <strong style="color:#f0f0f0">{arrow(total_change)} Total ROU {fmt_pct(total_change)} vs previous period.</strong>
            {"Best location: <strong style='color:#f0f0f0'>" + best_loc[0] + "</strong> (" + fmt_pct(best_loc[1]) + ")." if best_loc else ""}
            {"Best category: <strong style='color:#f0f0f0'>" + best_cat[0] + "</strong> (" + fmt_pct(best_cat[1]) + ")." if best_cat else ""}
            {"<br>Watch: <strong style='color:#f87171'>" + worst_cat[0] + "</strong> " + fmt_pct(worst_cat[1]) + "." if worst_cat and worst_cat[1] < 0 else ""}
          </p>
        </td>
      </tr>
      <tr><td style="height:20px"></td></tr>

      <!-- KPI cards -->
      <tr>
        <td>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td width="25%" style="padding-right:8px">
                <div style="background:#1a1a1a;border-radius:8px;padding:14px">
                  <p style="margin:0;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:0.08em">Total ROU</p>
                  <p style="margin:4px 0 0;font-size:20px;font-weight:500;color:#f0f0f0;font-family:monospace">{fmt_ron(today_data["grand_total"])}</p>
                  <p style="margin:3px 0 0;font-size:12px;color:{color(total_change)};font-family:monospace">{arrow(total_change)} {fmt_pct(total_change)}</p>
                </div>
              </td>
              <td width="25%" style="padding-right:8px">
                <div style="background:#1a1a1a;border-radius:8px;padding:14px">
                  <p style="margin:0;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:0.08em">Previous</p>
                  <p style="margin:4px 0 0;font-size:20px;font-weight:500;color:#666;font-family:monospace">{fmt_ron(prev_data["grand_total"])}</p>
                  <p style="margin:3px 0 0;font-size:12px;color:#444;font-family:monospace">baseline</p>
                </div>
              </td>
              <td width="25%" style="padding-right:8px">
                <div style="background:#1a1a1a;border-radius:8px;padding:14px">
                  <p style="margin:0;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:0.08em">Best location</p>
                  <p style="margin:4px 0 0;font-size:18px;font-weight:500;color:#f0f0f0">{best_loc[0] if best_loc else "—"}</p>
                  <p style="margin:3px 0 0;font-size:12px;color:#4ade80;font-family:monospace">{fmt_pct(best_loc[1]) if best_loc else ""}</p>
                </div>
              </td>
              <td width="25%">
                <div style="background:#1a1a1a;border-radius:8px;padding:14px">
                  <p style="margin:0;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:0.08em">Watch</p>
                  <p style="margin:4px 0 0;font-size:18px;font-weight:500;color:#f0f0f0">{worst_cat[0].split()[-1] if worst_cat else "—"}</p>
                  <p style="margin:3px 0 0;font-size:12px;color:{color(worst_cat[1]) if worst_cat else '#666'};font-family:monospace">{fmt_pct(worst_cat[1]) if worst_cat else ""}</p>
                </div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <tr><td style="height:24px"></td></tr>

      <!-- Section label -->
      <tr>
        <td style="padding-bottom:12px">
          <p style="margin:0;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:0.1em">Locations &amp; Categories</p>
          <hr style="border:none;border-top:1px solid #1e1e1e;margin:6px 0 0">
        </td>
      </tr>

      <!-- Location rows -->
      <tr>
        <td>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr style="background:transparent">
              <th style="text-align:left;font-size:10px;color:#333;text-transform:uppercase;padding:0 12px 8px;letter-spacing:0.08em">Location / Category</th>
              <th style="text-align:right;font-size:10px;color:#333;text-transform:uppercase;padding:0 12px 8px;letter-spacing:0.08em">{today_label}</th>
              <th style="text-align:right;font-size:10px;color:#333;text-transform:uppercase;padding:0 12px 8px;letter-spacing:0.08em">{prev_label}</th>
              <th style="text-align:right;font-size:10px;color:#333;text-transform:uppercase;padding:0 12px 8px;letter-spacing:0.08em">Change</th>
            </tr>
            {loc_rows_html}
          </table>
        </td>
      </tr>

      <!-- Footer -->
      <tr><td style="height:32px"></td></tr>
      <tr>
        <td style="border-top:1px solid #1a1a1a;padding-top:16px">
          <p style="margin:0;font-size:11px;color:#333;font-family:monospace">Generated automatically · VNR ROU Digest · {now_str}</p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""
    return html


# ── Step 4: Send the email ─────────────────────────────────────────────────
def send_email(html_content, today_label):
    msg = MIMEMultipart("alternative")
    now_str = datetime.now(BUCHAREST_TZ).strftime("%d %b %Y")
    msg["Subject"] = f"☀️ ROU Sales Digest — {now_str}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = SEND_TO
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        server.sendmail(GMAIL_ADDRESS, SEND_TO, msg.as_string())
    print(f"✅ Digest sent to {SEND_TO}")


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    print("🔄 Fetching today's report...")
    today_pdf = fetch_latest_pdf(days_back=0)
    print(f"   Got PDF: {len(today_pdf):,} bytes")

    print("🔄 Fetching previous report...")
    prev_pdf = fetch_latest_pdf(days_back=1)
    print(f"   Got PDF: {len(prev_pdf):,} bytes")

    print("🤖 Extracting today's data with Claude...")
    today_data = extract_sales_data(today_pdf, "today")
    print(f"   Found {len(today_data['locations'])} locations, grand total: {today_data['grand_total']:,.0f} RON")

    print("🤖 Extracting previous data with Claude...")
    prev_data = extract_sales_data(prev_pdf, "previous day")
    print(f"   Found {len(prev_data['locations'])} locations, grand total: {prev_data['grand_total']:,.0f} RON")

    today_label = today_data.get("report_date", "Today")
    prev_label = prev_data.get("report_date", "Previous")

    print("📧 Building email digest...")
    html = build_email_html(today_data, prev_data, today_label, prev_label)

    print("📤 Sending email...")
    send_email(html, today_label)
    print("🎉 Done!")


if __name__ == "__main__":
    main()
