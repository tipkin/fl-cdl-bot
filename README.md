# FL CDL Checker Bot — Railway Deployment

## Files
```
bot.py           ← main bot (Playwright-based)
Dockerfile       ← Railway build config
requirements.txt ← Python dependencies
railway.toml     ← Railway deployment config
```

---

## Step 1 — Push to GitHub

Create a private GitHub repo and push all 4 files:

```bash
git init
git add .
git commit -m "FL CDL bot"
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

---

## Step 2 — Deploy on Railway

1. Go to https://railway.app → New Project → Deploy from GitHub repo
2. Select your repo — Railway auto-detects the Dockerfile
3. Go to **Variables** tab and add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | `7861050375:AAGA4ZFHa0BVkJqziRdAh2M2Dv1a-mXBSCU` |
| `CAPTCHA_API_KEY` | `d532cc873ccef678c30cb26e27d33348` |

4. Click **Deploy** — build takes ~3–5 min (Playwright + Chromium download)

---

## Step 3 — Verify selectors (REQUIRED)

The FLHSMV site was redesigned. Before bulk use:

1. Send `/debug` to the bot from Telegram
2. Bot replies with a screenshot + list of all `<input>`, `<button>`, `<img>` elements
3. Find the correct IDs for:
   - DL number input field
   - CAPTCHA image (or check if reCAPTCHA is shown instead)
   - CAPTCHA text input
   - Submit button
4. Open `bot.py`, paste confirmed selectors at the **top** of each selector list
   (marked with comments like `# ── Confirmed new-portal selector ──`)
5. Commit & push → Railway redeploys automatically

---

## Usage

Send to the bot (one driver per line):

```
John Smith A305530960000
Daniel Montes De Oca C235160842070
```

Bot replies with screenshot + parsed status per driver.

---

## Commands

| Command | Description |
|---|---|
| `/debug` | Screenshot + element dump of FLHSMV page |
| `/myid` | Returns your Telegram user ID |

---

## Adding/removing allowed users

Edit `ALLOWED_IDS` set in `bot.py`, commit & push.
