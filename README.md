# Canvas Alerts

Daily email digests from Penn State Canvas LMS. Color-coded, urgency-aware alerts so you never miss a deadline.

## What You Get

**Morning Digest (8am ET):** Missed items, due today, due tomorrow, due in 2-3 days, new assignments, and recent announcements — all color-coded by urgency.

**Evening Pre-Alert (8pm ET):** Quick reminder of anything due tomorrow that you haven't submitted yet. Only sends if there's something to warn about.

## Deploy Your Own

### 1. Get a Canvas API Token

1. Log into [Canvas](https://psu.instructure.com)
2. Go to **Account → Settings**
3. Scroll to **Approved Integrations** → **+ New Access Token**
4. Give it a name, generate, and copy the token

### 2. Get a Gmail App Password

1. Enable 2FA on your Google account
2. Go to [App Passwords](https://myaccount.google.com/apppasswords)
3. Generate a new app password for "Mail"

### 3. Fork & Configure

1. Fork this repo
2. Go to **Settings → Secrets and variables → Actions**
3. Add these secrets:

| Secret | Value |
|--------|-------|
| `CANVAS_API_TOKEN` | Your Canvas token |
| `CANVAS_BASE_URL` | `https://psu.instructure.com/api/v1` |
| `EMAIL_SENDER` | Your Gmail address |
| `EMAIL_PASSWORD` | Your Gmail app password |
| `EMAIL_RECIPIENTS` | Comma-separated emails |

### 4. Enable Actions

Go to **Actions** tab and enable workflows. The digest runs automatically at 8am and 8pm ET.

### 5. Test It

Click **Actions → Canvas Digest → Run workflow** to trigger manually.

## Local Testing

```bash
cp .env.example .env
# fill in .env values
export $(cat .env | xargs)
pip install -r requirements.txt
python canvas_alerts.py
```

## How It Works

- Fetches all active courses, assignments, announcements, and calendar events from Canvas API
- Categorizes by urgency: missed → due today → due tomorrow → 2-3 days → new
- Detects new assignments you haven't seen before (tracked in `state.json`)
- Sends HTML email via Gmail SMTP
- `state.json` auto-commits after each run to track what you've already seen
- DST-aware scheduling with double-cron guards
