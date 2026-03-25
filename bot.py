import logging
import os
import re
import tempfile
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from twocaptcha import TwoCaptcha
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CAPTCHA_API_KEY    = os.environ["CAPTCHA_API_KEY"]

ALLOWED_IDS = {
    309536053, 7132305790, 1427494269, 1651701922, 7336134613,
    568654083,  7330403626, 7590844142, 7227816299, 6449193448,
    5336807983, 952178821,
}

solver = TwoCaptcha(CAPTCHA_API_KEY)

# ─────────────────────────────────────────────────────────────
#  STATE CONFIG
# ─────────────────────────────────────────────────────────────
STATES = {
    "FL": {
        "url": "https://mydmvportal.flhsmv.gov/home/en/publicweb/dlcheck",
        "dl_input":       ["input#DLNumber", "input[name='DriverLicenseNumber']"],
        "captcha_img":    ["img#dlCheckCaptcha_CaptchaImage", "img.LBD_CaptchaImage", "img[src*='BotDetectCaptcha' i]"],
        "captcha_input":  ["input#CaptchaCode", "input[name='CaptchaCode']"],
        "submit":         ["button#continueButton", "button[type='submit']", "button:has-text('Continue')"],
        "agree_checkbox": None,   # FL has no terms checkbox
        "captcha_img_src_pattern": "BotDetectCaptcha",
    },
    "CT": {
        "url": "https://www.dmvselfservice.ct.gov/LicenseStatusService.aspx?language=en_US",
        # Confirmed from page source:
        "dl_input":       ["input#txtDriverLicence"],          # note: "Licence" typo is correct
        "captcha_img":    ["img[src*='CaptchaImage.axd' i]"],
        "captcha_input":  ["input#ctl00_contentplaceholder_txtCap"],
        "submit":         ["input#ctl00_contentplaceholder_cmdSubmit"],
        "agree_checkbox": "input#ctl00_contentplaceholder_chkAgree",  # must be checked
        "captcha_img_src_pattern": "CaptchaImage.axd",
    },
}

RECAPTCHA_SELECTORS = [".g-recaptcha", "[data-sitekey]", "iframe[src*='recaptcha']"]


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────
async def find_first(page, selectors, timeout=8000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="attached", timeout=timeout)
            logger.info(f"Matched: {sel}")
            return loc
        except PlaywrightTimeout:
            continue
    return None


async def detect_recaptcha(page):
    for sel in RECAPTCHA_SELECTORS:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="attached", timeout=3000)
            key = await el.get_attribute("data-sitekey")
            if key:
                return key
            src = await el.get_attribute("src") or ""
            m = re.search(r"k=([A-Za-z0-9_-]+)", src)
            if m:
                return m.group(1)
        except PlaywrightTimeout:
            continue
    return None


def solve_image_captcha(image_path):
    try:
        result = solver.normal(image_path)
        logger.info(f"CAPTCHA solved: {result['code']}")
        return result["code"]
    except Exception as e:
        logger.error(f"2Captcha error: {e}")
        return None


def solve_recaptcha_v2(site_key, page_url):
    try:
        result = solver.recaptcha(sitekey=site_key, url=page_url)
        return result["code"]
    except Exception as e:
        logger.error(f"reCAPTCHA error: {e}")
        return None


# ─────────────────────────────────────────────────────────────
#  RESULT PARSERS
# ─────────────────────────────────────────────────────────────
async def parse_fl_result(page):
    try:
        text = " ".join(l.strip() for l in (await page.inner_text("body")).splitlines() if l.strip())

        if re.search(r"\bis\s+valid\b", text, re.I):
            status = "\u2705 STATUS: VALID \u2705"
        elif re.search(r"\b(cancelled|suspended|revoked|disqualified|withdrawn)\b", text, re.I):
            status = "\U0001f6a8 STATUS: INVALID / ACTION REQUIRED \U0001f6a8"
        else:
            status = "\u26a0\ufe0f STATUS: UNKNOWN"

        parts = [status]
        m = re.search(r"Class\s+(\w+)", text, re.I)
        if m: parts.append(f"Class: {m.group(1)}")
        m = re.search(r"expiration date of (\d{2}/\d{2}/\d{4})", text, re.I)
        if m: parts.append(f"Expires: {m.group(1)}")
        m = re.search(r"Medical Certification Expiration Date[:\s]+(\d{2}/\d{2}/\d{4})", text, re.I)
        if m: parts.append(f"Med Cert Exp: {m.group(1)}")

        for section in [
            "Effective Insurance Cancellation Suspensions",
            "Court Suspension",
            "Suspensions, Revocations, Cancellations, Disqualifications",
        ]:
            sm = re.search(re.escape(section) + r".{0,300}", text, re.I | re.DOTALL)
            if sm and "None on Record" not in sm.group(0):
                parts.append(f"\u26a0\ufe0f {section}: see screenshot")

        return "\n".join(parts)
    except Exception as e:
        logger.error(f"FL parse error: {e}")
        return "\u26a0\ufe0f Could not parse result — check screenshot"


async def parse_ct_result(page):
    try:
        text = " ".join(l.strip() for l in (await page.inner_text("body")).splitlines() if l.strip())

        if re.search(r"(not found|no record|invalid credential|does not exist)", text, re.I):
            status = "\u274c STATUS: NOT FOUND / INVALID NUMBER"
        elif re.search(r"\bvalid\b", text, re.I) and not re.search(r"\b(suspended|revoked|cancelled|disqualified)\b", text, re.I):
            status = "\u2705 STATUS: VALID \u2705"
        elif re.search(r"\b(suspended|revoked|cancelled|disqualified)\b", text, re.I):
            status = "\U0001f6a8 STATUS: INVALID / ACTION REQUIRED \U0001f6a8"
        else:
            status = "\u26a0\ufe0f STATUS: UNKNOWN"

        parts = [status]
        m = re.search(r"[Ee]xpir\w+[:\s]+(\d{1,2}/\d{1,2}/\d{4})", text)
        if m: parts.append(f"Expires: {m.group(1)}")
        m = re.search(r"[Cc]lass[:\s]+([A-D]\b)", text)
        if m: parts.append(f"Class: {m.group(1)}")
        m = re.search(r"[Ee]ndorsement[s]?[:\s]+([A-Z0-9, ]+)", text)
        if m: parts.append(f"Endorsements: {m.group(1).strip()}")

        return "\n".join(parts)
    except Exception as e:
        logger.error(f"CT parse error: {e}")
        return "\u26a0\ufe0f Could not parse CT result — check screenshot"


PARSERS = {"FL": parse_fl_result, "CT": parse_ct_result}


# ─────────────────────────────────────────────────────────────
#  CORE CHECK
# ─────────────────────────────────────────────────────────────
async def check_cdl(driver_name, cdl_number, state, update):
    cfg = STATES[state]
    result_path = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await context.new_page()

        try:
            await page.goto(cfg["url"], wait_until="networkidle", timeout=30000)

            # 1. DL number
            dl_input = await find_first(page, cfg["dl_input"])
            if not dl_input:
                await update.message.reply_text(f"\u274c {state} {cdl_number}: DL input not found. Run /debug{state.lower()}")
                return
            await dl_input.fill(cdl_number)

            # 2. Terms checkbox (CT only)
            if cfg["agree_checkbox"]:
                try:
                    chk = page.locator(cfg["agree_checkbox"]).first
                    await chk.wait_for(state="attached", timeout=5000)
                    await chk.check()
                    # Also set the hidden txtAgreed field directly as backup
                    await page.evaluate(
                        "document.getElementById('ctl00_contentplaceholder_txtAgreed').value = 'Agreed';"
                    )
                    logger.info("CT: terms checkbox checked")
                except Exception as e:
                    logger.warning(f"CT agree checkbox error: {e}")

            # 3. CAPTCHA
            recaptcha_key = await detect_recaptcha(page)
            if recaptcha_key:
                token = solve_recaptcha_v2(recaptcha_key, cfg["url"])
                if not token:
                    await update.message.reply_text(f"\u274c {state} {cdl_number}: reCAPTCHA failed")
                    return
                await page.evaluate(
                    f"document.getElementById('g-recaptcha-response').innerHTML = '{token}';"
                )
            else:
                captcha_img = await find_first(page, cfg["captcha_img"], timeout=5000)
                if captcha_img:
                    # Reload captcha
                    await page.evaluate("el => el.src = el.src + '&r=' + Math.random()", await captcha_img.element_handle())
                    await page.wait_for_timeout(2000)
                    captcha_img = await find_first(page, cfg["captcha_img"], timeout=5000)

                    captcha_path = os.path.join(tempfile.gettempdir(), f"captcha_{state}.png")
                    await captcha_img.screenshot(path=captcha_path)
                    captcha_text = solve_image_captcha(captcha_path)
                    if not captcha_text:
                        await update.message.reply_text(f"\u274c {state} {cdl_number}: CAPTCHA not solved")
                        return

                    cap_input = await find_first(page, cfg["captcha_input"])
                    if not cap_input:
                        await update.message.reply_text(f"\u274c {state} {cdl_number}: CAPTCHA input not found")
                        return
                    await cap_input.fill(captcha_text)
                else:
                    logger.warning(f"{state}: no CAPTCHA found — continuing")

            # 4. Submit
            submit = await find_first(page, cfg["submit"])
            if not submit:
                await update.message.reply_text(f"\u274c {state} {cdl_number}: Submit button not found. Run /debug{state.lower()}")
                return
            await submit.click()
            await page.wait_for_timeout(3000)

            # 5. Screenshot + parse
            result_path = os.path.join(tempfile.gettempdir(), f"result_{state}_{cdl_number}.png")
            await page.screenshot(path=result_path, full_page=False)
            parsed = await PARSERS[state](page)

            caption = (
                f"\U0001f464 {driver_name} \U0001f464\n"
                f"{state} | {cdl_number}\n"
                f"{datetime.now().strftime('%m/%d/%Y')}\n\n"
                f"{parsed}"
            )
            with open(result_path, "rb") as f:
                await update.message.reply_photo(f, caption=caption)

        except Exception as e:
            logger.error(f"Error {state} {cdl_number}: {e}", exc_info=True)
            await update.message.reply_text(f"\u26a0\ufe0f {state} {cdl_number} error: {e}")
        finally:
            await browser.close()
            if result_path and os.path.exists(result_path):
                os.remove(result_path)


# ─────────────────────────────────────────────────────────────
#  DEBUG COMMAND
# ─────────────────────────────────────────────────────────────
async def run_debug(update, state_code):
    if getattr(update.effective_user, "id", None) not in ALLOWED_IDS:
        return
    if state_code not in STATES:
        await update.message.reply_text(f"Unknown state: {state_code}")
        return

    cfg = STATES[state_code]
    await update.message.reply_text(f"\U0001f50d Loading {state_code} page...")
    debug_path = os.path.join(tempfile.gettempdir(), f"debug_{state_code}.png")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            await page.goto(cfg["url"], wait_until="networkidle", timeout=30000)
            await page.screenshot(path=debug_path, full_page=True)

            inputs  = await page.query_selector_all("input")
            buttons = await page.query_selector_all("button")
            imgs    = await page.query_selector_all("img")
            rckey   = await detect_recaptcha(page)

            lines = [f"STATE: {state_code}", f"URL: {page.url}", "", "-- INPUTS --"]
            for el in inputs[:25]:
                lines.append(f"  id='{await el.get_attribute('id')}' name='{await el.get_attribute('name')}' type='{await el.get_attribute('type')}'")
            lines.append("\n-- BUTTONS --")
            for el in buttons[:10]:
                lines.append(f"  id='{await el.get_attribute('id')}' type='{await el.get_attribute('type')}' text='{(await el.inner_text())[:40]}'")
            lines.append("\n-- IMAGES --")
            for el in imgs[:10]:
                lines.append(f"  id='{await el.get_attribute('id')}' src='{(await el.get_attribute('src') or '')[:80]}'")
            lines.append(f"\n-- reCAPTCHA: {rckey or 'NOT FOUND'} --")

            await update.message.reply_text(f"```\n{chr(10).join(lines)[:3800]}\n```", parse_mode="Markdown")
            with open(debug_path, "rb") as f:
                await update.message.reply_photo(f, caption=f"{state_code} page screenshot")
        except Exception as e:
            await update.message.reply_text(f"Debug error: {e}")
        finally:
            await browser.close()
            if os.path.exists(debug_path):
                os.remove(debug_path)


async def debug_fl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_debug(update, "FL")

async def debug_ct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_debug(update, "CT")


# ─────────────────────────────────────────────────────────────
#  BULK HANDLER
#  Format: FIRSTNAME LASTNAME LICENSENUMBER [FL|CT]
#  State defaults to FL if omitted (backward compatible)
# ─────────────────────────────────────────────────────────────
async def handle_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = getattr(update.effective_user, "id", None)
    if uid not in ALLOWED_IDS:
        logger.warning(f"Blocked uid={uid}")
        return
    if not update.message or not update.message.text:
        return

    for line in update.message.text.strip().split("\n"):
        parts = line.strip().split()
        if len(parts) < 2:
            await update.message.reply_text(f"\u274c Wrong format: '{line}'\nExpected: NAME LICENSENUMBER [FL|CT]")
            continue

        state = "FL"
        if parts[-1].upper() in STATES:
            state = parts[-1].upper()
            parts = parts[:-1]

        if len(parts) < 2:
            await update.message.reply_text(f"\u274c Wrong format: '{line}'\nExpected: NAME LICENSENUMBER [FL|CT]")
            continue

        cdl_number  = parts[-1]
        driver_name = " ".join(parts[:-1])
        await check_cdl(driver_name, cdl_number, state, update)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(str(update.effective_user.id))

async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning(f"Blocked uid={getattr(update.effective_user, 'id', None)}")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("myid",    cmd_myid))
    app.add_handler(CommandHandler("debugfl", debug_fl))
    app.add_handler(CommandHandler("debugct", debug_ct))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(user_id=list(ALLOWED_IDS)),
        handle_bulk,
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.User(user_id=list(ALLOWED_IDS)),
        deny,
    ))
    logger.info("FL/CT CDL Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
