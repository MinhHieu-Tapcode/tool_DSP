"""Playwright automation client for Pancake POS GIP 306 order export."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import Page, async_playwright

PANCAKE_BASE_URL = "https://pos.pancake.vn"
PANCAKE_DASHBOARD_URL = f"{PANCAKE_BASE_URL}/dashboard"
GIP306_SHOP_ID = "1329065531"
GIP306_FILTER_ID = "5297043a-6697-4fe2-bfd0-91e87f29e4e9"
GIP306_ORDER_FILTER_URL = (
    f"{PANCAKE_BASE_URL}/shop/{GIP306_SHOP_ID}/order?saveFilterId={GIP306_FILTER_ID}"
)
PANCAKE_PRESET_MATCH = "check trùng data với p"
PANCAKE_STATE_PATH = Path(__file__).resolve().parent / "pancake_storage_state.json"

_TAB_COUNT_RE = re.compile(r"\bMới\s+(\d+)\b", re.IGNORECASE)


async def interactive_login(
    state_path: Path = PANCAKE_STATE_PATH,
    timeout_sec: int = 240,
    logger: Optional[logging.Logger] = None,
) -> Path:
    """Open headed browser so user can solve Pancake login/CAPTCHA once and save session."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1360, "height": 840})
        page = await context.new_page()
        await page.goto(PANCAKE_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
        if logger:
            logger.info(
                "Đã mở trình duyệt Pancake POS. Vui lòng đăng nhập (+ CAPTCHA nếu có) trong vòng %ds...",
                timeout_sec,
            )
        await page.wait_for_function(
            """() => {
                const u = window.location.href || "";
                const txt = document.body ? document.body.innerText : "";
                return u.startsWith("https://pos.pancake.vn/") &&
                       !u.includes("account.pancake.vn") &&
                       (txt.includes("Danh sách cửa hàng") || txt.includes("GIP 306") || u.includes("/shop/"));
            }""",
            timeout=timeout_sec * 1000,
        )
        await page.wait_for_timeout(1500)
        await context.storage_state(path=str(state_path))
        await browser.close()
    if logger:
        logger.info("Đã lưu phiên Pancake POS vào %s", state_path)
    return state_path


async def _wait_table_idle(page: Page) -> None:
    await page.wait_for_timeout(800)
    try:
        await page.wait_for_function(
            """() => !document.querySelector('.ant-spin-spinning')""",
            timeout=20000,
        )
    except Exception:
        pass


async def _select_custom_export_preset(page: Page, preset_match: str) -> None:
    modal = page.locator(".ant-modal-content, [role='dialog']").filter(has_text="Xuất").last
    await modal.wait_for(state="visible", timeout=15000)

    # 1) Under "Chọn mẫu file", switch to "Tùy chỉnh" if not already active
    file_mode_select = modal.locator(".ant-select").nth(0)
    cur_mode = (await file_mode_select.inner_text()).strip()
    if "Tùy chỉnh" not in cur_mode:
        await file_mode_select.click()
        opt = page.locator(".ant-select-item-option").filter(has_text="Tùy chỉnh").first
        await opt.wait_for(state="visible", timeout=10000)
        await opt.click()
        await page.wait_for_timeout(500)

    # 2) Under the custom template dropdown (2nd select), pick preset matching "check trùng data với p"
    preset_select = modal.locator(".ant-select").nth(1)
    cur_preset = (await preset_select.inner_text()).strip().lower()
    if preset_match.lower() not in cur_preset:
        await preset_select.click()
        preset_opt = page.locator(".ant-select-item-option").filter(has_text=re.compile(re.escape(preset_match), re.IGNORECASE)).first
        await preset_opt.wait_for(state="visible", timeout=10000)
        await preset_opt.click()
        await page.wait_for_timeout(500)


async def export_gip306_new_orders(
    output_dir: Path,
    run_ts: datetime,
    state_path: Path = PANCAKE_STATE_PATH,
    headless: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Tuple[Optional[Path], int]:
    """Export 'LỌC LOẠI TRỪ -> Mới' orders from Pancake POS GIP 306 using the custom preset."""
    if not state_path.exists():
        raise RuntimeError(
            f"Chưa có phiên đăng nhập Pancake POS ({state_path}). "
            "Chạy 'python -m gip306.run_luong1 --login' để đăng nhập 1 lần."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(state_path),
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = await context.new_page()
        try:
            await page.goto(GIP306_ORDER_FILTER_URL, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(2000)
            if "account.pancake.vn" in page.url or "login" in page.url.lower():
                raise RuntimeError(
                    "Phiên Pancake POS đã hết hạn. Chạy 'python -m gip306.run_luong1 --login' để đăng nhập lại."
                )

            await _wait_table_idle(page)

            # Click tab "Mới"
            moi_tab = page.locator("button, div[role='tab'], span").filter(
                has_text=re.compile(r"^\s*Mới\s+\d+\s*$")
            ).first
            await moi_tab.wait_for(state="visible", timeout=20000)
            tab_text = (await moi_tab.inner_text()).strip()
            m_cnt = _TAB_COUNT_RE.search(tab_text)
            count = int(m_cnt.group(1)) if m_cnt else 0
            if logger:
                logger.info("Pancake POS GIP 306 (%s) — Tab '%s'", GIP306_SHOP_ID, tab_text)
            if count == 0:
                await context.storage_state(path=str(state_path))
                return None, 0

            await moi_tab.click()
            await _wait_table_idle(page)

            # Select all visible rows on current page (1000/page)
            select_all = page.locator("thead input[type='checkbox']").first
            await select_all.wait_for(state="visible", timeout=15000)
            await select_all.check(force=True)
            await page.wait_for_timeout(500)

            # Click "Xuất" button in top-right toolbar
            xuat_btn = page.get_by_role("button", name=re.compile(r"^\s*Xuất\s*$")).first
            await xuat_btn.click()

            await _select_custom_export_preset(page, PANCAKE_PRESET_MATCH)

            modal = page.locator(".ant-modal-content, [role='dialog']").filter(has_text="Xuất").last
            download_btn = modal.get_by_role("button", name=re.compile(r"Tải xuống")).first
            async with page.expect_download(timeout=60000) as dl_info:
                await download_btn.click()
            download = await dl_info.value

            xlsx_path = output_dir / f"gip306_pos_{run_ts.strftime('%Y%m%d_%H%M%S')}.xlsx"
            await download.save_as(str(xlsx_path))
            await context.storage_state(path=str(state_path))
            if logger:
                logger.info("Đã tải Excel Pancake POS (%d đơn): %s", count, xlsx_path)
            return xlsx_path, count
        finally:
            await browser.close()
