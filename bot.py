import logging
import os
import re
import tempfile
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from twocaptcha import TwoCaptcha
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# LOGGING
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# CONFIG
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CAPTCHA_API_KEY    = os.environ["CAPTCHA_API_KEY"]

ALLOWED_IDS = {
    309536053, 7132305790, 1427494269, 1651701922, 7336134613,
    568654083,  7330403626, 7590844142, 7227816299, 6449193448,
    5336807983, 952178821,
}

DL_CHECK_URL = "https://mydmvportal.flhsmv.gov/home/en/publicweb/dlcheck"

solver = TwoCaptcha(CAPTCHA_API_KEY)

# CONFIRMED SELECTORS
DL_INPUT_SELECTORS = [
    "input#DLNumber",
    "input[name='DriverLicenseNumber']",
    "input[placeholder*='License' i]",
]

CAPTCHA_IMG_SELECTORS = [
    "img#dlCheckCaptcha_CaptchaImage",
    "img.LBD_CaptchaImage",
    "img[src*='BotDetectCaptcha' i]",
    "img[src*='captcha' i]",
]

CAPTCHA_INPUT_SELECTORS = [
    "input#CaptchaCode",
    "input[name='CaptchaCode']",
    "input[id*='captcha' i]",
    "input[name*='captcha' i]",
]

SUBMIT_SELECTORS = [
    "button#continueButton",
    "button[type='submit']",
    "button:has-text('Continue')",
]

RECAPTCHA_SELECTORS = [
    ".g-recaptcha",
    "[data-sitekey]",
    "iframe[src*='recaptcha']",
]


# BROWSER HELPERS
async def find_first(page, selectors, timeout=8000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="attached", timeout=timeout)
            logger.info(f"Matched selector: {sel}")
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


# CAPTCHA SOLVERS
def solve_image_captcha(image_path):
    try:
        result = solver.normal(image_path)
        logger.info(f"Image CAPTCHA solved: {result['code']}")
        return result["code"]
    except Exception as e:
        logger.error(f"2Captcha image error: {e}")
        return None


def solve_recaptcha_v2(site_key, page_url):
    try:
        result = solver.recaptcha(sitekey=site_key, url=page_url)
        logger.info("reCAPTCHA v2 solved")
        return result["code"]
    except Exception as e:
        logger.error(f"2Captcha reCAPTCHA error: {e}")
        return None


# RESULT PAGE PARSER - reads HTML directly, no OCR needed
async def parse_result_page(page):
    try:
        body = await page.inner_text("body")
        lines = [l.strip() for l in body.splitlines() if l.strip()]
        text = " ".join(lines)

        is_valid = bool(re.search(r"\bis\s+valid\b", text, re.I))
        is_bad = bool(re.search(
            r"\b(cancelled|suspended|revoked|disqualified|withdrawn)\b", text, re.I
        ))

        if is_valid:
            status_emoji = "\u2705 STATUS: VALID \u2705"
        elif is_bad:
            status_emoji = "\U0001f6a8 STATUS: INVALID / ACTION REQUIRED \U0001f6a8"
        else:
            status_emoji = "\u26a0\ufe0f STATUS: UNKNOWN"

        parts = [status_emoji]

        m = re.search(r"Class\s+(\w+)", text, re.I)
        if m:
            parts.append(f"Class: {m.group(1)}")

        m = re.search(r"expiration date of (\d{2}/\d{2}/\d{4})", text, re.I)
        if m:
            parts.append(f"Expires: {m.group(1)}")

        m = re.search(r"Medical Certification Expiration Date[:\s]+(\d{2}/\d{2}/\d{4})", text, re.I)
        if m:
            parts.append(f"Med Cert Exp: {m.group(1)}")

        issue_sections = [
            "Effective Insurance Cancellation Suspensions",
            "Court Suspension",
            "Suspensions, Revocations, Cancellations, Disqualifications",
        ]
        for section in issue_sections:
            pattern = re.escape(section) + r".{0,300}"
            sm = re.search(pattern, text, re.I | re.DOTALL)
            if sm and "None on Record" not in sm.group(0):
                parts.append(f"\u26a0\ufe0f {section}: see screenshot")

        return "\n".join(parts)

    except Exception as e:
        logger.error(f"Page parse error: {e}")
        return "\u26a0\ufe0f Could not parse result page — check screenshot"


# CORE CHECK
async def check_cdl(driver_name, cdl_number, update):
    result_path = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()

        try:
            # 1. Load page
            await page.goto(DL_CHECK_URL, wait_until="networkidle", timeout=30000)

            # 2. Enter DL number
            dl_input = await find_first(page, DL_INPUT_SELECTORS)
            if not dl_input:
                await update.message.reply_text(
                    f"\u274c {cdl_number}: DL input field not found. Run /debug."
                )
                return
            await dl_input.fill(cdl_number)

            # 3. CAPTCHA
            recaptcha_key = await detect_recaptcha(page)

            if recaptcha_key:
                token = solve_recaptcha_v2(recaptcha_key, DL_CHECK_URL)
                if not token:
                    await update.message.reply_text(f"\u274c {cdl_number}: reCAPTCHA failed")
                    return
                await page.evaluate(
                    f"document.getElementById('g-recaptcha-response').innerHTML = '{token}';"
                )
            else:
                captcha_img = await find_first(page, CAPTCHA_IMG_SELECTORS, timeout=5000)
                if captcha_img:
                    await page.evaluate(
                        "el => el.src = el.src + '&r=' + Math.random()",
                        await captcha_img.element_handle(),
                    )
                    await page.wait_for_timeout(2000)
                    captcha_img = await find_first(page, CAPTCHA_IMG_SELECTORS, timeout=5000)

                    captcha_path = os.path.join(tempfile.gettempdir(), "captcha.png")
                    await captcha_img.screenshot(path=captcha_path)
                    captcha_text = solve_image_captcha(captcha_path)
                    if not captcha_text:
                        await update.message.reply_text(f"\u274c {cdl_number}: CAPTCHA not solved")
                        return

                    cap_input = await find_first(page, CAPTCHA_INPUT_SELECTORS)
                    if not cap_input:
                        await update.message.reply_text(
                            f"\u274c {cdl_number}: CAPTCHA input not found. Run /debug."
                        )
                        return
                    await cap_input.fill(captcha_text)
                else:
                    logger.warning("No CAPTCHA found — continuing")

            # 4. Submit
            submit = await find_first(page, SUBMIT_SELECTORS)
            if not submit:
                await update.message.reply_text(
                    f"\u274c {cdl_number}: Submit button not found. Run /debug."
                )
                return
            await submit.click()
            await page.wait_for_timeout(3000)

            # 5. Screenshot + parse HTML directly
            result_path = os.path.join(tempfile.gettempdir(), f"result_{cdl_number}.png")
            await page.screenshot(path=result_path, full_page=False)

            parsed = await parse_result_page(page)

            caption = (
                f"\U0001f464 {driver_name} \U0001f464\n"
                f"{cdl_number}\n"
                f"{datetime.now().strftime('%m/%d/%Y')}\n\n"
                f"{parsed}"
            )
            with open(result_path, "rb") as f:
                await update.message.reply_photo(f, caption=caption)

        except Exception as e:
            logger.error(f"Error for {cdl_number}: {e}", exc_info=True)
            await update.message.reply_text(f"\u26a0\ufe0f {cdl_number} error: {e}")
        finally:
            await browser.close()
            if result_path and os.path.exists(result_path):
                os.remove(result_path)


# /debug COMMAND
async def debug_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if getattr(update.effective_user, "id", None) not in ALLOWED_IDS:
        return
    await update.message.reply_text("\U0001f50d Loading page for diagnostics...")

    debug_path = os.path.join(tempfile.gettempdir(), "debug.png")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            await page.goto(DL_CHECK_URL, wait_until="networkidle", timeout=30000)
            await page.screenshot(path=debug_path, full_page=True)

            inputs  = await page.query_selector_all("input")
            buttons = await page.query_selector_all("button")
            imgs    = await page.query_selector_all("img")
            rckey   = await detect_recaptcha(page)

            lines = [f"URL: {page.url}", ""]
            lines.append("-- INPUTS --")
            for el in inputs[:25]:
                lines.append(
                    f"  id='{await el.get_attribute('id')}' "
                    f"name='{await el.get_attribute('name')}' "
                    f"type='{await el.get_attribute('type')}' "
                    f"placeholder='{await el.get_attribute('placeholder')}'"
                )
            lines.append("\n-- BUTTONS --")
            for el in buttons[:10]:
                lines.append(
                    f"  id='{await el.get_attribute('id')}' "
                    f"type='{await el.get_attribute('type')}' "
                    f"text='{(await el.inner_text())[:40]}'"
                )
            lines.append("\n-- IMAGES --")
            for el in imgs[:10]:
                src = (await el.get_attribute("src") or "")[:80]
                lines.append(
                    f"  id='{await el.get_attribute('id')}' "
                    f"class='{await el.get_attribute('class')}' "
                    f"src='{src}'"
                )
            lines.append(f"\n-- reCAPTCHA site-key: {rckey or 'NOT FOUND'} --")

            report = "\n".join(lines)
            await update.message.reply_text(f"```\n{report[:3800]}\n```", parse_mode="Markdown")
            with open(debug_path, "rb") as f:
                await update.message.reply_photo(f, caption="DL Check page screenshot")

        except Exception as e:
            await update.message.reply_text(f"Debug error: {e}")
        finally:
            await browser.close()
            if os.path.exists(debug_path):
                os.remove(debug_path)


# HANDLERS
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
            await update.message.reply_text(
                f"\u274c Wrong format: '{line}'\nExpected: FIRSTNAME LASTNAME LICENSENUMBER"
            )
            continue
        cdl_number  = parts[-1]
        driver_name = " ".join(parts[:-1])
        await check_cdl(driver_name, cdl_number, update)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(str(update.effective_user.id))


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning(f"Blocked uid={getattr(update.effective_user, 'id', None)}")


# MAIN
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("myid",  cmd_myid))
    app.add_handler(CommandHandler("debug", debug_page))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.User(user_id=list(ALLOWED_IDS)),
            handle_bulk,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.User(user_id=list(ALLOWED_IDS)),
            deny,
        )
    )
    logger.info("FL CDL Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
