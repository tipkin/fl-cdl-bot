import logging
import os
import re
import time
import tempfile
from datetime import datetime

import pytesseract
from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from twocaptcha import TwoCaptcha
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  CONFIG  (set as Railway env vars — never commit real values)
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CAPTCHA_API_KEY    = os.environ["CAPTCHA_API_KEY"]

ALLOWED_IDS = {
    309536053, 7132305790, 1427494269, 1651701922, 7336134613,
    568654083,  7330403626, 7590844142, 7227816299, 6449193448,
    5336807983, 952178821,
}

DL_CHECK_URL = "https://mydmvportal.flhsmv.gov/home/en/publicweb/dlcheck"

solver = TwoCaptcha(CAPTCHA_API_KEY)

# ─────────────────────────────────────────────────────────────
#  SELECTOR LISTS  (ranked: most-likely-correct first)
#  After running /debug, paste the confirmed selector at [0]
# ─────────────────────────────────────────────────────────────
DL_INPUT_SELECTORS = [
    # ── Confirmed new-portal selector (fill in after /debug) ──
    # "input#NEW_ID",
    # ── Legacy ────────────────────────────────────────────────
    "input#MainContent_txtDLNumber",
    "input#txtDLNumber",
    "input[name='DLNumber']",
    "input[name='dlNumber']",
    "input[placeholder*='License' i]",
    "input[id*='DLNumber' i]",
    "input[id*='dlnumber' i]",
]

CAPTCHA_IMG_SELECTORS = [
    # ── Confirmed new-portal selector (fill in after /debug) ──
    # "img.NEW_CLASS",
    # ── Legacy ────────────────────────────────────────────────
    "img.LBD_CaptchaImage",
    "img[src*='captcha' i]",
    "img[id*='captcha' i]",
    "img[class*='captcha' i]",
]

CAPTCHA_INPUT_SELECTORS = [
    # ── Confirmed new-portal selector (fill in after /debug) ──
    # "input#NEW_ID",
    # ── Legacy ────────────────────────────────────────────────
    "input#MainContent_txtCaptchaCode",
    "input#txtCaptchaCode",
    "input[id*='captcha' i]",
    "input[name*='captcha' i]",
    "input[placeholder*='captcha' i]",
]

SUBMIT_SELECTORS = [
    # ── Confirmed new-portal selector (fill in after /debug) ──
    # "input#NEW_ID",
    # ── Legacy ────────────────────────────────────────────────
    "input#MainContent_btnEnter",
    "input#btnEnter",
    "input[type='submit']",
    "button[type='submit']",
    "button:has-text('Enter')",
    "button:has-text('Check')",
    "button:has-text('Submit')",
]

# ─────────────────────────────────────────────────────────────
#  BROWSER HELPERS
# ─────────────────────────────────────────────────────────────
async def find_first(page, selectors: list[str], timeout: int = 8000):
    """Return the first locator that resolves within timeout ms."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="attached", timeout=timeout)
            logger.info(f"Matched selector: {sel}")
            return loc
        except PlaywrightTimeout:
            continue
    return None


async def detect_recaptcha(page) -> str | None:
    """Return reCAPTCHA site-key if present, else None."""
    for sel in [".g-recaptcha", "[data-sitekey]", "iframe[src*='recaptcha']"]:
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


# ─────────────────────────────────────────────────────────────
#  CAPTCHA SOLVERS
# ─────────────────────────────────────────────────────────────
def solve_image_captcha(image_path: str) -> str | None:
    try:
        result = solver.normal(image_path)
        logger.info(f"Image CAPTCHA solved: {result['code']}")
        return result["code"]
    except Exception as e:
        logger.error(f"2Captcha image error: {e}")
        return None


def solve_recaptcha_v2(site_key: str, page_url: str) -> str | None:
    try:
        result = solver.recaptcha(sitekey=site_key, url=page_url)
        logger.info("reCAPTCHA v2 solved")
        return result["code"]
    except Exception as e:
        logger.error(f"2Captcha reCAPTCHA error: {e}")
        return None


# ─────────────────────────────────────────────────────────────
#  OCR + RESULT PARSER
# ─────────────────────────────────────────────────────────────
def extract_text(path: str) -> str:
    try:
        return pytesseract.image_to_string(Image.open(path), lang="eng")
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return ""


def parse_result(raw: str) -> str:
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return "ℹ️ OCR: empty result"

    if any("VALID" in l.upper() for l in lines):
        return "✅ STATUS: VALID ✅"

    dates = []
    for line in lines:
        m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", line)
        if m:
            dates.append(f"  {m.group(0)}")

    if dates:
        return "🚨 STATUS: UPCOMING ACTION 🚨\n" + "\n".join(dates)

    return "ℹ️ Status not detected\n\nRaw OCR:\n" + "\n".join(lines[:12])


# ─────────────────────────────────────────────────────────────
#  CORE CHECK
# ─────────────────────────────────────────────────────────────
async def check_cdl(driver_name: str, cdl_number: str, update: Update):
    result_path = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        # Mask automation flag
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()

        try:
            # ── 1. Load ──────────────────────────────────────
            await page.goto(DL_CHECK_URL, wait_until="networkidle", timeout=30000)

            # ── 2. DL number ─────────────────────────────────
            dl_input = await find_first(page, DL_INPUT_SELECTORS)
            if not dl_input:
                await update.message.reply_text(
                    f"❌ {cdl_number}: поле ввода номера не найдено.\n"
                    "Запустите /debug для диагностики."
                )
                return
            await dl_input.fill(cdl_number)

            # ── 3. CAPTCHA ───────────────────────────────────
            recaptcha_key = await detect_recaptcha(page)

            if recaptcha_key:
                token = solve_recaptcha_v2(recaptcha_key, DL_CHECK_URL)
                if not token:
                    await update.message.reply_text(f"❌ {cdl_number}: reCAPTCHA не решена")
                    return
                await page.evaluate(
                    f"""
                    document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                    var cb = document.querySelector('.g-recaptcha')?.getAttribute('data-callback');
                    if (cb && window[cb]) window[cb]('{token}');
                    """
                )
            else:
                captcha_img = await find_first(page, CAPTCHA_IMG_SELECTORS, timeout=5000)
                if captcha_img:
                    # Reload captcha image
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
                        await update.message.reply_text(f"❌ {cdl_number}: капча не решена")
                        return

                    cap_input = await find_first(page, CAPTCHA_INPUT_SELECTORS)
                    if not cap_input:
                        await update.message.reply_text(
                            f"❌ {cdl_number}: поле капчи не найдено.\n"
                            "Запустите /debug для диагностики."
                        )
                        return
                    await cap_input.fill(captcha_text)
                else:
                    logger.warning("No CAPTCHA element found — continuing without it")

            # ── 4. Submit ────────────────────────────────────
            submit = await find_first(page, SUBMIT_SELECTORS)
            if not submit:
                await update.message.reply_text(
                    f"❌ {cdl_number}: кнопка Submit не найдена.\n"
                    "Запустите /debug для диагностики."
                )
                return
            await submit.click()
            await page.wait_for_timeout(3000)

            # ── 5. Screenshot + OCR ──────────────────────────
            result_path = os.path.join(tempfile.gettempdir(), f"result_{cdl_number}.png")
            await page.screenshot(path=result_path, full_page=False)

            ocr_raw = extract_text(result_path)
            parsed  = parse_result(ocr_raw)

            caption = (
                f"👤 {driver_name} 👤\n"
                f"{cdl_number}\n"
                f"{datetime.now().strftime('%m/%d/%Y')}\n\n"
                f"{parsed}"
            )
            with open(result_path, "rb") as f:
                await update.message.reply_photo(f, caption=caption)

        except Exception as e:
            logger.error(f"Error for {cdl_number}: {e}", exc_info=True)
            await update.message.reply_text(f"⚠️ {cdl_number} ошибка: {e}")
        finally:
            await browser.close()
            if result_path and os.path.exists(result_path):
                os.remove(result_path)


# ─────────────────────────────────────────────────────────────
#  /debug  — page screenshot + full element dump
# ─────────────────────────────────────────────────────────────
async def debug_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if getattr(update.effective_user, "id", None) not in ALLOWED_IDS:
        return
    await update.message.reply_text("🔍 Загружаю страницу...")

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

            lines.append("── INPUTS ──")
            for el in inputs[:25]:
                lines.append(
                    f"  id='{await el.get_attribute('id')}' "
                    f"name='{await el.get_attribute('name')}' "
                    f"type='{await el.get_attribute('type')}' "
                    f"placeholder='{await el.get_attribute('placeholder')}'"
                )

            lines.append("\n── BUTTONS ──")
            for el in buttons[:10]:
                lines.append(
                    f"  id='{await el.get_attribute('id')}' "
                    f"type='{await el.get_attribute('type')}' "
                    f"text='{(await el.inner_text())[:40]}'"
                )

            lines.append("\n── IMAGES ──")
            for el in imgs[:10]:
                src = (await el.get_attribute("src") or "")[:80]
                lines.append(
                    f"  id='{await el.get_attribute('id')}' "
                    f"class='{await el.get_attribute('class')}' "
                    f"src='{src}'"
                )

            lines.append(f"\n── reCAPTCHA site-key: {rckey or 'NOT FOUND'} ──")

            report = "\n".join(lines)
            await update.message.reply_text(f"```\n{report[:3800]}\n```", parse_mode="Markdown")
            with open(debug_path, "rb") as f:
                await update.message.reply_photo(f, caption="Скриншот DL Check page")

        except Exception as e:
            await update.message.reply_text(f"Debug error: {e}")
        finally:
            await browser.close()
            if os.path.exists(debug_path):
                os.remove(debug_path)


# ─────────────────────────────────────────────────────────────
#  BULK CHECK HANDLER
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
            await update.message.reply_text(f"❌ Неверный формат: '{line}'\nОжидается: ИМЯ ФАМИЛИЯ НОМЕР")
            continue
        cdl_number  = parts[-1]
        driver_name = " ".join(parts[:-1])
        await check_cdl(driver_name, cdl_number, update)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(str(update.effective_user.id))


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.warning(f"Blocked uid={getattr(update.effective_user,'id',None)}")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
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
