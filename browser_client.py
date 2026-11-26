"""Async Playwright automation client for G-solution (headless/headed, multi-tab)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.parse
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from playwright.async_api import Browser, BrowserContext, Locator, Page, async_playwright

from classifier import (
    OrderRecord,
    PhoneHistory,
    has_test_order_flag,
    is_empty_order_fields,
    is_within_days,
    normalize_phone,
    parse_created_at,
    parse_return_rate,
    parse_return_rate_tooltip,
    phone_search_variants,
)
from config import (
    DUPLICATE_WINDOW_DAYS,
    GIP287_CANCELABLE_SMALL_STATUSES,
    GIP287_RETURN_RATE_THRESHOLD,
    GIP287_SHOP_LABEL,
    GIP287_SHOP_QUERY,
    GSOLUTION_LOGIN_URL,
    GSOLUTION_ORDER_URL,
    GSOLUTION_TELESALE_URL,
    MARKET_COUNTRY_CODE,
    STEP2_POS_SHOPS,
    STEP2_RETURN_RATE_THRESHOLD,
    STORAGE_STATE_PATH,
    TAG_DUPLICATE,
    TAG_GROUP,
    TAG_NEED_PAY_ADVANCED,
    TAG_TEST,
    get_credentials,
)

LOGGER = logging.getLogger("gsolution_auto")

_TAB_COUNT_RE = re.compile(
    r"^(All|New|Need attention|Restocking|Need confirmation|Pending|Confirmed|Wait Print|Printed|Packing|Waiting for pick up|Shipped|Delivered|Returning|Returned|Canceled)\s*[·\s]*(\d+)",
    re.IGNORECASE,
)


def parse_tab_counts(tab_texts: Sequence[str]) -> Dict[str, int]:
    """Parse Ant Design tab innerTexts into {tab_name: count}."""
    counts: Dict[str, int] = {}
    for raw in tab_texts:
        cleaned = re.sub(r"\s+", " ", raw).strip()
        m = _TAB_COUNT_RE.match(cleaned)
        if m:
            counts[m.group(1).strip()] = int(m.group(2))
    return counts


def _normalize_tab_key(counts: Dict[str, int], name: str) -> int:
    for k, v in counts.items():
        if k.lower() == name.lower():
            return v
    return 0


class GSolutionClient:
    """Manages Playwright Chromium browser, session persistence, scraping, and bulk updates."""

    def __init__(self, headless: bool = True, concurrency: int = 4) -> None:
        self.headless = headless
        self.concurrency = max(1, min(concurrency, 8))
        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def __aenter__(self) -> "GSolutionClient":
        self._pw = await async_playwright().start()
        launch_errors: List[str] = []
        for kwargs in (
            {"channel": "chrome", "headless": self.headless},
            {"channel": "msedge", "headless": self.headless},
            {"headless": self.headless},
        ):
            try:
                self.browser = await self._pw.chromium.launch(**kwargs)
                break
            except Exception as exc:
                launch_errors.append(f"{kwargs.get('channel', 'chromium')}: {exc}")
        if self.browser is None:
            raise RuntimeError("Không khởi động được trình duyệt Chromium/Chrome: " + " | ".join(launch_errors))

        ctx_kwargs: Dict[str, object] = {"viewport": {"width": 1440, "height": 900}}
        if STORAGE_STATE_PATH.exists():
            ctx_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        self.context = await self.browser.new_context(**ctx_kwargs)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(15000)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self.context is not None:
                await self.context.close()
        finally:
            if self.browser is not None:
                await self.browser.close()
            if self._pw is not None:
                await self._pw.stop()

    async def ensure_logged_in(self) -> None:
        """Open /admin/order and authenticate via .env credentials if redirected to /login."""
        assert self.page is not None and self.context is not None
        await self.page.goto(GSOLUTION_ORDER_URL, wait_until="domcontentloaded")
        await self.page.locator("form, table, main").first.wait_for(state="visible", timeout=15000)

        if "/login" in self.page.url:
            LOGGER.info("Phiên đăng nhập chưa có hoặc đã hết hạn; đang đăng nhập lại...")
            email, password = get_credentials()

            body_text = (await self.page.locator("body").inner_text()).lower()
            if any(k in body_text for k in ("captcha", "verify you are human", "turnstile", "2fa", "otp")):
                raise RuntimeError("Trang đăng nhập xuất hiện CAPTCHA hoặc xác thực 2 bước; dừng tự động.")

            user_input = self.page.locator('input:not([type="password"])').first
            pass_input = self.page.locator('input[type="password"]').first
            await user_input.fill(email)
            await pass_input.fill(password)
            await self.page.get_by_role("button", name="Login", exact=True).click()

            try:
                await self.page.wait_for_url(lambda u: "/admin" in u, timeout=15000)
            except Exception as exc:
                err_text = await self.page.locator("body").inner_text()
                if "Wrong username or password" in err_text:
                    raise RuntimeError("Đăng nhập G-solution thất bại: sai email hoặc mật khẩu trong .env.") from exc
                raise RuntimeError(f"Không chuyển được vào trang /admin sau đăng nhập (URL hiện tại: {self.page.url}).") from exc

            await self.page.wait_for_load_state("domcontentloaded")
            await self.page.goto(GSOLUTION_ORDER_URL, wait_until="domcontentloaded")
            await self.page.locator("main").first.wait_for(state="visible", timeout=15000)

        await self._ensure_market_ph(self.page)
        tmp_state = STORAGE_STATE_PATH.with_name(f"{STORAGE_STATE_PATH.name}.{os.getpid()}.tmp")
        await self.context.storage_state(path=str(tmp_state))
        os.replace(tmp_state, STORAGE_STATE_PATH)
        LOGGER.info("Đã xác thực G-solution và lưu session vào storage_state.json.")

    async def _wait_table_settled(self, page: Page, timeout_ms: int = 15000) -> None:
        """Wait for Ant Design table spinner to finish."""
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
        await asyncio.sleep(0.25)
        while asyncio.get_running_loop().time() < deadline:
            spin = await page.locator(".ant-spin-spinning").count()
            if spin == 0:
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.25)

    async def _ensure_market_ph(self, page: Page) -> None:
        """Verify market header is set to PH."""
        header = page.locator("header, .ant-layout-header").first
        try:
            txt = await header.inner_text()
        except Exception:
            txt = ""
        if MARKET_COUNTRY_CODE not in txt:
            LOGGER.warning("Không thấy mã thị trường %s rõ trên header; kiểm tra thủ công nếu cần.", MARKET_COUNTRY_CODE)

    async def _clear_all_filters(self, page: Page) -> None:
        """Remove active filter chips or search keyword if present."""
        # Clear keyword input if non-empty
        search_box = page.get_by_placeholder("Order ID").first
        if await search_box.count() > 0:
            val = await search_box.input_value()
            if val.strip():
                await search_box.fill("")
                await search_box.press("Enter")
                await self._wait_table_settled(page)

        # Click close icon on visible filter tags until none remain
        for _ in range(15):
            closes = page.locator("main .ant-tag .anticon-close, main .ant-tag [aria-label='close']")
            if await closes.count() == 0:
                # Try parent bar above main too
                closes = page.locator(".ant-tag .anticon-close, .ant-tag [aria-label='close']")
            cnt = await closes.count()
            if cnt == 0:
                break
            try:
                await closes.first.click(timeout=2000)
                await asyncio.sleep(0.2)
            except Exception:
                break
        await self._wait_table_settled(page)

    async def _open_add_filter_popup(self, page: Page) -> Locator:
        """Click Add Filter and return the filter menu portal container (with hydration retry)."""
        btn = page.locator("button").filter(has_text="Add Filter").first
        await btn.wait_for(state="visible", timeout=10000)
        for attempt in range(2):
            await btn.click()
            popup = page.locator("body > div").filter(has_text="Saved Filter").last
            try:
                await popup.get_by_text("Saved Filter", exact=True).first.wait_for(
                    state="visible", timeout=4000 if attempt == 0 else 8000
                )
                return popup
            except Exception:
                if attempt == 1:
                    raise
                await asyncio.sleep(1.0)
        return page.locator("body > div").filter(has_text="Saved Filter").last

    async def _close_popup(self, page: Page) -> None:
        await page.keyboard.press("Escape")
        try:
            hdr = page.locator("header, .ant-layout-header").first
            if await hdr.count() > 0:
                await hdr.click(position={"x": 450, "y": 20})
            else:
                await page.mouse.click(500, 18)
        except Exception:
            pass
        await asyncio.sleep(0.25)

    async def _select_tab(self, page: Page, tab_prefix: str) -> Dict[str, int]:
        """Select a status tab by name prefix and wait for rows to settle."""
        pat = re.compile(rf"^{re.escape(tab_prefix)}\b", re.I)
        tab = page.get_by_role("tab", name=pat).first
        await tab.wait_for(state="visible", timeout=15000)
        await tab.click()
        await page.get_by_role("tab", name=pat, selected=True).first.wait_for(state="visible", timeout=10000)
        await self._wait_table_settled(page)
        tabs = await page.locator('[role="tab"]').all_inner_texts()
        return parse_tab_counts(tabs)

    async def _set_page_size_1000(self, page: Page) -> None:
        """Switch pagination size to 1000 / page if available and not already 1000."""
        changer = page.locator(".ant-pagination-options-size-changer, .ant-pagination-options .ant-select-selector").first
        if await changer.count() == 0:
            return
        try:
            cur = await changer.inner_text()
        except Exception:
            cur = ""
        if "1000" in cur:
            return
        await changer.click()
        opt = page.locator(".ant-select-item-option").filter(has_text="1000 / page").first
        await opt.wait_for(state="visible", timeout=5000)
        await opt.click()
        await self._wait_table_settled(page)

    async def _ensure_return_rate_column(self, page: Page) -> None:
        """Make sure Return Rate column is enabled in the /admin/order table."""
        headers = [h.strip() for h in await page.locator("main table th").all_inner_texts()]
        if "Return Rate" in headers:
            return
        # Click table customize icon (next to Refresh button)
        col_btn = page.locator("main button:has(.anticon-table), main button:has([aria-label='table']), button:has([aria-label='table'])").first
        await col_btn.click()
        modal = page.locator(".ant-modal-content:visible").last
        await modal.wait_for(state="visible", timeout=5000)
        search_field = modal.get_by_placeholder("Search field")
        await search_field.fill("Return")
        await asyncio.sleep(0.2)
        rr_label = modal.locator("label, .ant-checkbox-wrapper").filter(has_text=re.compile(r"Return\s*Rate", re.I)).first
        if await rr_label.count() > 0:
            cb = rr_label.locator('input[type="checkbox"]')
            if not await cb.is_checked():
                await rr_label.click()
        await modal.get_by_role("button", name="OK").click()
        await modal.wait_for(state="hidden", timeout=5000)
        await self._wait_table_settled(page)

    async def _scrape_current_page_virtual_table(
        self,
        page: Page,
        expected_total: Optional[int] = None,
        max_scroll_steps: int = 160,
    ) -> List[OrderRecord]:
        """Fast virtual-scroll scraper using dynamic column headers and single-evaluate row reads."""
        no_data = await page.locator("main").get_by_text("No data", exact=True).count()
        if no_data > 0 and await page.locator('main input[type="checkbox"]').count() <= 1:
            return []

        for _ in range(25):
            if await page.locator('main input[type="checkbox"]').count() > 1:
                break
            await asyncio.sleep(0.2)

        if await page.locator('main input[type="checkbox"]').count() <= 1:
            return []

        meta = await page.evaluate(
            """() => {
                const ths = Array.from(document.querySelectorAll('main table th')).map(el => el.innerText.trim());
                const cbs = document.querySelectorAll('main input[type="checkbox"]');
                let rowDepth = 4;
                if (cbs.length > 1) {
                    let p = cbs[1];
                    for (let d = 1; d <= 7; d++) {
                        p = p.parentElement;
                        if (!p) break;
                        const divCount = Array.from(p.children).filter(c => c.tagName === 'DIV').length;
                        if (divCount >= Math.max(8, ths.length - 2)) {
                            rowDepth = d;
                            break;
                        }
                    }
                }
                const mainEl = document.querySelector('main');
                const rect = mainEl ? mainEl.getBoundingClientRect() : {left: 100, top: 250, width: 800, height: 500};
                return { headers: ths, rowDepth, rect: { x: rect.left + 220, y: rect.top + 260 } };
            }"""
        )

        headers: List[str] = meta["headers"]
        row_depth: int = meta["rowDepth"]
        mouse_pos = meta["rect"]

        def col_idx(name: str) -> int:
            try:
                return headers.index(name)
            except ValueError:
                return -1

        idx_map = {
            "id": col_idx("ID"),
            "shop": col_idx("Shop"),
            "svc": col_idx("Service Types"),
            "tags": col_idx("Tags"),
            "assign": col_idx("Assigning Sale"),
            "note": col_idx("Note"),
            "inote": col_idx("Internal Note"),
            "nprint": col_idx("Note Print"),
            "name": col_idx("Customer Name"),
            "phone": col_idx("Phone Number"),
            "addr": col_idx("Shipping Information"),
            "prod": col_idx("Products"),
            "created": col_idx("Created At"),
            "updated": col_idx("Updated At"),
            "status": col_idx("Status"),
            "rr": col_idx("Return Rate"),
        }

        if idx_map["id"] < 0:
            raise RuntimeError(f"Không tìm thấy cột 'ID' trong bảng (headers={headers}).")

        seen: Dict[str, OrderRecord] = {}
        no_new_count = 0

        await page.evaluate(
            "() => { const h = document.querySelector('.ant-table-tbody-virtual-holder'); if (h) h.scrollTop = 0; }"
        )
        await asyncio.sleep(0.15)

        for _ in range(max_scroll_steps):
            eval_res = await page.evaluate(
                """({ rowDepth, minCols }) => {
                    const cbs = Array.from(document.querySelectorAll('main input[type="checkbox"]')).slice(1);
                    const out = [];
                    for (const cb of cbs) {
                        let row = cb;
                        for (let d = 0; d < rowDepth; d++) {
                            if (!row) break;
                            row = row.parentElement;
                        }
                        if (!row) continue;
                        const divs = Array.from(row.children).filter(c => c.tagName === 'DIV');
                        if (divs.length < minCols) continue;
                        out.push(divs.map(el => el.innerText.trim()));
                    }
                    const h = document.querySelector('.ant-table-tbody-virtual-holder');
                    let hasHolder = Boolean(h);
                    let wasAtBottom = false;
                    if (h) {
                        wasAtBottom = (h.scrollTop + h.clientHeight >= h.scrollHeight - 2);
                        if (!wasAtBottom) {
                            const prev = h.scrollTop;
                            h.scrollTop = Math.min(h.scrollHeight, prev + Math.max(220, Math.floor(h.clientHeight * 0.65)));
                            if (h.scrollTop === prev) wasAtBottom = true;
                        }
                    }
                    return { out, hasHolder, wasAtBottom };
                }""",
                {"rowDepth": row_depth, "minCols": max(6, len(headers) - 2)},
            )
            batch = eval_res["out"]

            added = 0
            for vals in batch:
                def get_val(key: str) -> str:
                    i = idx_map[key]
                    return vals[i] if 0 <= i < len(vals) else ""

                oid = get_val("id")
                if not oid or oid in seen:
                    continue

                tags_raw = get_val("tags")
                tags: List[str] = []
                if tags_raw and tags_raw != "Add tag":
                    tags = [t.strip() for t in re.split(r"\s*·\s*|\n+", tags_raw) if t.strip()]

                seen[oid] = OrderRecord(
                    id=oid,
                    shop=get_val("shop"),
                    service_types=get_val("svc"),
                    tags=tags,
                    assigning_sale=get_val("assign"),
                    note=get_val("note"),
                    internal_note=get_val("inote"),
                    note_print=get_val("nprint"),
                    customer_name=get_val("name"),
                    phone=get_val("phone"),
                    address=get_val("addr"),
                    products=get_val("prod"),
                    created_at=get_val("created"),
                    updated_at=get_val("updated"),
                    status=get_val("status"),
                    return_rate=parse_return_rate(get_val("rr")),
                )
                added += 1

            if expected_total is not None and len(seen) >= expected_total:
                break

            if eval_res["hasHolder"]:
                if eval_res["wasAtBottom"]:
                    break
            else:
                no_new_count = no_new_count + 1 if added == 0 else 0
                if no_new_count >= 5:
                    break
                await page.mouse.move(mouse_pos["x"], mouse_pos["y"])
                await page.mouse.wheel(0, 360)

            await asyncio.sleep(0.14)

        return list(seen.values())

    async def _scrape_all_pages_virtual_table(
        self,
        page: Page,
        expected_total: Optional[int] = None,
    ) -> List[OrderRecord]:
        """Scrape across all pagination pages (after setting 1000 / page)."""
        await self._set_page_size_1000(page)
        all_rows: Dict[str, OrderRecord] = {}
        while True:
            page_rows = await self._scrape_current_page_virtual_table(
                page,
                expected_total=(min(1000, expected_total - len(all_rows)) if expected_total else None),
            )
            for r in page_rows:
                all_rows[r.id] = r
            next_btn = page.locator("li.ant-pagination-next:not(.ant-pagination-disabled)").first
            if await next_btn.count() == 0:
                break
            await next_btn.click()
            await self._wait_table_settled(page)
        return list(all_rows.values())

    # ------------------------------------------------------------------
    # Step 2: 3 GIPs Return Rate >= 50%
    # ------------------------------------------------------------------
    async def scan_step2_orders(self) -> Tuple[int, List[OrderRecord]]:
        """Open /admin/order, filter the 3 GIP shops on tab New, and scrape all rows with Return Rate."""
        assert self.page is not None
        page = self.page
        await page.goto(GSOLUTION_ORDER_URL, wait_until="domcontentloaded")
        await page.locator("main").first.wait_for(state="visible", timeout=15000)
        await self._clear_all_filters(page)

        popup = await self._open_add_filter_popup(page)
        await popup.get_by_text("POS Shop", exact=True).first.click()
        search_input = popup.get_by_placeholder("Search POS Shop")
        await search_input.wait_for(state="visible", timeout=5000)

        for query, exact_label in STEP2_POS_SHOPS:
            await search_input.fill(query)
            opt = popup.get_by_text(re.compile(re.escape(exact_label), re.I)).first
            await opt.wait_for(state="visible", timeout=5000)
            await opt.click()

        await self._close_popup(page)
        tab_counts = await self._select_tab(page, "New")
        total_new = _normalize_tab_key(tab_counts, "New")
        await self._ensure_return_rate_column(page)
        rows = await self._scrape_all_pages_virtual_table(page, expected_total=total_new)
        return total_new, rows

    # ------------------------------------------------------------------
    # Step 3: Telesale FILL IN DATA (Duplicated Phone & Not Duplicated Phone)
    # ------------------------------------------------------------------
    async def scan_telesale_lists(self) -> Tuple[int, List[OrderRecord], int, List[OrderRecord]]:
        """Scrape FILL_IN New orders from both Duplicated Phone and Not Duplicated Phone."""
        assert self.page is not None
        page = self.page
        await page.goto(GSOLUTION_TELESALE_URL, wait_until="domcontentloaded")
        await page.locator("main").first.wait_for(state="visible", timeout=15000)
        await self._clear_all_filters(page)

        # Apply Saved Filter -> FILL IN DATA
        popup = await self._open_add_filter_popup(page)
        await popup.get_by_text("Saved Filter", exact=True).first.click()
        fill_opt = popup.get_by_text("FILL IN DATA", exact=True).first
        await fill_opt.wait_for(state="visible", timeout=5000)
        await fill_opt.click()
        await self._close_popup(page)

        # 3A: Duplicated Phone -> Duplicated Phone
        popup = await self._open_add_filter_popup(page)
        await popup.get_by_text("Duplicated Phone", exact=True).first.click()
        dup_opts = popup.get_by_text("Duplicated Phone", exact=True)
        cnt = await dup_opts.count()
        await (dup_opts.nth(1) if cnt >= 2 else dup_opts.first).click()
        await self._close_popup(page)

        tabs_3a = await self._select_tab(page, "New")
        total_3a = _normalize_tab_key(tabs_3a, "New")
        all_3a = await self._scrape_all_pages_virtual_table(page, expected_total=total_3a)
        fillin_3a = [r for r in all_3a if "FILL_IN" in r.service_types.upper()]

        # Remove Duplicated Phone chip and switch to Not Duplicated Phone
        dup_chip_close = page.locator(".ant-tag").filter(has_text=re.compile(r"Duplicated Phone", re.I)).locator(".anticon-close, [aria-label='close']").first
        if await dup_chip_close.count() > 0:
            await dup_chip_close.click()
            await self._wait_table_settled(page)

        popup = await self._open_add_filter_popup(page)
        await popup.get_by_text("Duplicated Phone", exact=True).first.click()
        not_dup_opt = popup.get_by_text("Not Duplicated Phone", exact=True).first
        await not_dup_opt.wait_for(state="visible", timeout=5000)
        await not_dup_opt.click()
        await self._close_popup(page)

        tabs_3b = await self._select_tab(page, "New")
        total_3b = _normalize_tab_key(tabs_3b, "New")
        all_3b = await self._scrape_all_pages_virtual_table(page, expected_total=total_3b)
        fillin_3b = [r for r in all_3b if "FILL_IN" in r.service_types.upper()]

        return total_3a, fillin_3a, total_3b, fillin_3b

    # ------------------------------------------------------------------
    # Concurrent Phone History Lookups
    # ------------------------------------------------------------------
    async def _lookup_single_phone_on_page(self, page: Page, raw_phone: str) -> PhoneHistory:
        """Query /admin/order for a phone number (trying 09... and +63... when needed)."""
        norm = normalize_phone(raw_phone)
        variants = phone_search_variants(raw_phone)
        merged_orders: Dict[str, OrderRecord] = {}
        best_tabs: Dict[str, int] = {}
        best_all = 0
        best_returned = 0
        any_incomplete = False

        for idx, query in enumerate(variants):
            # Query 09... first; only also query +63... if first returned < 2 or raw started with +63 and <= 30
            if idx > 0 and best_all >= 2 and not raw_phone.strip().startswith("+63"):
                break
            if idx > 0 and best_all > 30:
                # 09... already returned superset (>30 orders), skip redundant +63 re-scroll
                break

            url = f"{GSOLUTION_ORDER_URL}?keyword={urllib.parse.quote(query, safe='')}"
            await page.goto(url, wait_until="domcontentloaded")
            await page.locator('[role="tab"]').first.wait_for(state="visible", timeout=15000)
            await self._wait_table_settled(page)

            tabs_text = await page.locator('[role="tab"]').all_inner_texts()
            tab_counts = parse_tab_counts(tabs_text)
            all_cnt = _normalize_tab_key(tab_counts, "All")
            ret_cnt = _normalize_tab_key(tab_counts, "Returned")

            if all_cnt > best_all:
                best_all = all_cnt
                best_tabs = tab_counts
            if ret_cnt > best_returned:
                best_returned = ret_cnt

            if all_cnt == 0:
                continue

            rows = await self._scrape_all_pages_virtual_table(page, expected_total=all_cnt)
            if len(rows) < all_cnt:
                any_incomplete = True
            for r in rows:
                merged_orders[r.id] = r

        orders_list = list(merged_orders.values())
        has_test = any(has_test_order_flag(o)[0] == "DEFINITE" for o in orders_list)

        return PhoneHistory(
            normalized_phone=norm,
            all_count=max(best_all, len(orders_list)),
            returned_count=max(best_returned, sum(1 for o in orders_list if o.status == "Returned")),
            tab_counts=best_tabs,
            orders=orders_list,
            has_test_hit=has_test,
            incomplete_scan=any_incomplete,
        )

    async def lookup_phones_concurrent(self, phone_samples: Sequence[str]) -> Dict[str, PhoneHistory]:
        """Look up full phone histories concurrently across N browser tabs."""
        assert self.context is not None
        unique_by_norm: Dict[str, str] = {}
        for p in phone_samples:
            norm = normalize_phone(p)
            if norm and norm not in unique_by_norm:
                unique_by_norm[norm] = p

        queue: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()
        for norm, sample in unique_by_norm.items():
            queue.put_nowait((norm, sample))

        results: Dict[str, PhoneHistory] = {}
        worker_count = min(self.concurrency, max(1, len(unique_by_norm)))
        pages = [await self.context.new_page() for _ in range(worker_count)]
        for pg in pages:
            pg.set_default_timeout(15000)

        async def worker(pg: Page) -> None:
            while not queue.empty():
                try:
                    norm, sample = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    hist = await self._lookup_single_phone_on_page(pg, sample)
                    results[norm] = hist
                except Exception as exc:
                    LOGGER.error("Lỗi tra cứu SĐT %s: %s", sample, exc)
                    results[norm] = PhoneHistory(normalized_phone=norm, incomplete_scan=True)
                finally:
                    queue.task_done()

        try:
            await asyncio.gather(*(worker(pg) for pg in pages))
        finally:
            for pg in pages:
                await pg.close()

        return results

    # ------------------------------------------------------------------
    # Empty Order Detail Modal Inspection (e.g., JEELKMD)
    # ------------------------------------------------------------------
    async def inspect_empty_order_detail(self, order_id: str) -> bool:
        """Open /admin/order?keyword=<order_id> modal strictly read-only to confirm Empty Cart & empty notes."""
        assert self.context is not None
        pg = await self.context.new_page()
        pg.set_default_timeout(15000)
        try:
            url = f"{GSOLUTION_ORDER_URL}?keyword={urllib.parse.quote(order_id, safe='')}"
            await pg.goto(url, wait_until="domcontentloaded")
            await pg.locator('[role="tab"]').first.wait_for(state="visible", timeout=15000)
            await self._wait_table_settled(pg)
            for _ in range(20):
                if await pg.locator('main input[type="checkbox"]').count() > 1:
                    break
                await asyncio.sleep(0.2)
            holder = pg.locator(".ant-table-tbody-virtual-holder")
            if await holder.count() > 0:
                row = holder.locator(".ant-table-row").filter(has_text=order_id).first
            else:
                row = pg.locator("main .ant-table-row").filter(has_text=order_id).first
            await row.locator("> div").nth(2).click(timeout=5000)

            modal = pg.locator(".ant-modal-content:visible").last
            await modal.wait_for(state="visible", timeout=8000)
            modal_text = await modal.inner_text()
            inputs = await modal.locator("input, textarea").all()
            non_empty_notes = False
            for el in inputs:
                try:
                    placeholder = (await el.get_attribute("placeholder") or "").lower()
                    val = (await el.input_value()).strip()
                    if any(k in placeholder for k in ("note", "ghi chú")) and val:
                        non_empty_notes = True
                except Exception:
                    pass

            has_empty_cart = "Empty Cart" in modal_text
            close_btn = modal.locator("button.ant-modal-close").first
            if await close_btn.is_visible():
                await close_btn.click()
            await modal.wait_for(state="hidden", timeout=5000)
            return has_empty_cart and not non_empty_notes
        except Exception as exc:
            LOGGER.warning("Không kiểm tra được chi tiết đơn %s: %s", order_id, exc)
            return False
        finally:
            await pg.close()

    # ------------------------------------------------------------------
    # Safe Mutation Helpers (--apply mode only)
    # ------------------------------------------------------------------
    async def _prepare_telesale_fill_in_new(self, page: Page) -> None:
        """Open /admin/order/telesale with Saved Filter: FILL IN DATA (no Duplicated Phone chip) on tab New."""
        await page.goto(GSOLUTION_TELESALE_URL, wait_until="domcontentloaded")
        await page.locator("main").first.wait_for(state="visible", timeout=15000)
        await self._clear_all_filters(page)

        popup = await self._open_add_filter_popup(page)
        await popup.get_by_text("Saved Filter", exact=True).first.click()
        fill_opt = popup.get_by_text("FILL IN DATA", exact=True).first
        await fill_opt.wait_for(state="visible", timeout=5000)
        await fill_opt.click()
        await self._close_popup(page)

        await self._select_tab(page, "New")
        await self._set_page_size_1000(page)

    async def _select_exact_ids(
        self,
        page: Page,
        target_ids: Set[str],
        require_unassigned: bool = True,
        require_fillin: bool = True,
        allowed_statuses: Sequence[str] = ("New",),
    ) -> Tuple[List[str], List[str]]:
        """Scroll through virtual table and check ONLY exact target_ids matching allowed_statuses."""
        if not target_ids:
            return [], []

        meta = await page.evaluate(
            """() => {
                const ths = Array.from(document.querySelectorAll('main table th')).map(el => el.innerText.trim());
                const cbs = document.querySelectorAll('main input[type="checkbox"]');
                let rowDepth = 4;
                if (cbs.length > 1) {
                    let p = cbs[1];
                    for (let d = 1; d <= 7; d++) {
                        p = p.parentElement;
                        if (!p) break;
                        const divCount = Array.from(p.children).filter(c => c.tagName === 'DIV').length;
                        if (divCount >= Math.max(8, ths.length - 2)) {
                            rowDepth = d;
                            break;
                        }
                    }
                }
                const mainEl = document.querySelector('main');
                const rect = mainEl ? mainEl.getBoundingClientRect() : {left: 100, top: 250};
                return { headers: ths, rowDepth, rect: { x: rect.left + 220, y: rect.top + 260 } };
            }"""
        )

        headers: List[str] = meta["headers"]
        row_depth: int = meta["rowDepth"]
        mouse_pos = meta["rect"]
        i_id = headers.index("ID")
        i_svc = headers.index("Service Types") if "Service Types" in headers else -1
        i_assign = headers.index("Assigning Sale") if "Assigning Sale" in headers else -1
        i_status = headers.index("Status") if "Status" in headers else -1

        # Clear any lingering selection pill first
        sel_pill = page.locator("main").get_by_text(re.compile(r"^\d+\s+Selected$")).first
        if await sel_pill.count() > 0 and await sel_pill.is_visible():
            pill_close = sel_pill.locator("..").locator("span[aria-label='close'], .anticon-close, img").last
            if await pill_close.count() > 0:
                await pill_close.click()
                await asyncio.sleep(0.15)

        await page.evaluate(
            "() => { const h = document.querySelector('.ant-table-tbody-virtual-holder'); if (h) h.scrollTop = 0; }"
        )
        await asyncio.sleep(0.2)

        checked_ids: List[str] = []
        checked_set: Set[str] = set()
        skipped_set: Set[str] = set()
        seen_ids: Set[str] = set()
        max_steps = max(250, len(target_ids) * 2 + 120)
        no_new = 0
        retry_clicks: Dict[str, int] = {}

        for _ in range(max_steps):
            step_res = await page.evaluate(
                """({ rowDepth, minCols, iId, iSvc, iAssign, iStatus, targets, checkedIds, skippedIds, requireUnassigned, requireFillin, allowedStatuses }) => {
                    const targetSet = new Set(targets);
                    const checkedSet = new Set(checkedIds);
                    const skippedSet = new Set(skippedIds);
                    const statusSet = new Set(allowedStatuses);
                    const cbs = Array.from(document.querySelectorAll('main input[type="checkbox"]')).slice(1);
                    const newlyChecked = [];
                    const newlySkipped = [];
                    const visibleIds = [];
                    let pendingCb = null;
                    let pendingOid = null;

                    for (const cb of cbs) {
                        let row = cb;
                        for (let d = 0; d < rowDepth; d++) {
                            if (!row) break;
                            row = row.parentElement;
                        }
                        if (!row) continue;
                        const divs = Array.from(row.children).filter(c => c.tagName === 'DIV');
                        if (divs.length < minCols) continue;

                        const oid = (divs[iId]?.innerText || '').trim();
                        if (!oid) continue;
                        visibleIds.push(oid);
                        if (!targetSet.has(oid) || skippedSet.has(oid)) continue;

                        const svc = iSvc >= 0 ? (divs[iSvc]?.innerText || '').trim() : '';
                        const assign = iAssign >= 0 ? (divs[iAssign]?.innerText || '').trim() : '';
                        const status = iStatus >= 0 ? (divs[iStatus]?.innerText || '').trim() : '';

                        const liveOk = (
                            (statusSet.size === 0 || statusSet.has(status))
                            && (!requireUnassigned || assign === '')
                            && (!requireFillin || svc.toUpperCase().includes('FILL_IN'))
                        );
                        if (!liveOk) {
                            newlySkipped.push(oid);
                            skippedSet.add(oid);
                            continue;
                        }
                        if (cb.checked) {
                            if (!checkedSet.has(oid)) {
                                newlyChecked.push(oid);
                                checkedSet.add(oid);
                            }
                        } else if (!pendingCb) {
                            pendingCb = cb;
                            pendingOid = oid;
                        }
                    }

                    if (pendingCb) {
                        pendingCb.click();
                        return { newlyChecked, newlySkipped, visibleIds, clickedOne: pendingOid, hasHolder: true, wasAtBottom: false };
                    }

                    const h = document.querySelector('.ant-table-tbody-virtual-holder');
                    const hasHolder = Boolean(h);
                    let wasAtBottom = false;
                    if (h) {
                        wasAtBottom = (h.scrollTop + h.clientHeight >= h.scrollHeight - 2);
                        if (!wasAtBottom) {
                            const prev = h.scrollTop;
                            h.scrollTop = Math.min(h.scrollHeight, prev + Math.max(200, Math.floor(h.clientHeight * 0.65)));
                            if (h.scrollTop === prev) wasAtBottom = true;
                        }
                    }
                    return { newlyChecked, newlySkipped, visibleIds, clickedOne: null, hasHolder, wasAtBottom };
                }""",
                {
                    "rowDepth": row_depth,
                    "minCols": max(6, len(headers) - 2),
                    "iId": i_id,
                    "iSvc": i_svc,
                    "iAssign": i_assign,
                    "iStatus": i_status,
                    "targets": list(target_ids),
                    "checkedIds": checked_ids,
                    "skippedIds": sorted(skipped_set),
                    "requireUnassigned": require_unassigned,
                    "requireFillin": require_fillin,
                    "allowedStatuses": list(allowed_statuses),
                },
            )

            for oid in step_res["newlyChecked"]:
                if oid not in checked_set:
                    checked_set.add(oid)
                    checked_ids.append(oid)
            skipped_set.update(step_res["newlySkipped"])
            new_seen = sum(1 for oid in step_res["visibleIds"] if oid not in seen_ids)
            seen_ids.update(step_res["visibleIds"])

            clicked_one = step_res["clickedOne"]
            if clicked_one:
                retry_clicks[clicked_one] = retry_clicks.get(clicked_one, 0) + 1
                if retry_clicks[clicked_one] >= 4:
                    skipped_set.add(clicked_one)
                await asyncio.sleep(0.04)
                continue

            if len(checked_ids) + len(skipped_set) >= len(target_ids):
                break

            if step_res["hasHolder"]:
                if step_res["wasAtBottom"]:
                    break
            else:
                no_new = no_new + 1 if new_seen == 0 else 0
                if no_new >= 5:
                    break
                await page.mouse.move(mouse_pos["x"], mouse_pos["y"])
                await page.mouse.wheel(0, 320)

            await asyncio.sleep(0.15)

        for oid in target_ids:
            if oid not in checked_set and oid not in skipped_set:
                skipped_set.add(oid)

        return checked_ids, sorted(skipped_set)

    async def _open_bulk_modal_and_verify(
        self,
        page: Page,
        expected_count: int,
        expected_ids: Optional[Sequence[str]] = None,
    ) -> Locator:
        """Click the rocket Bulk Update Order button and verify modal opened with exact selection."""
        rocket = page.locator(
            'main button:has([aria-label="rocket"]), main button:has(.anticon-rocket), main .anticon-rocket'
        ).first
        if await rocket.count() == 0:
            # Fallback to More -> Bulk update order
            more_btn = page.locator("main button").filter(has_text=re.compile(r"^More\b", re.I)).first
            await more_btn.click()
            await page.get_by_text(re.compile(r"Bulk update order", re.I)).first.click()
        else:
            await rocket.click()

        modal = page.locator(".ant-modal-content:visible").last
        await modal.wait_for(state="visible", timeout=8000)
        await asyncio.sleep(0.45)
        txt = await modal.inner_text()
        if not re.search(rf"\b{expected_count}\s+Selected\b", txt):
            raise RuntimeError(
                f"Số đơn được chọn trong Bulk Update Order không khớp ({expected_count} Selected): {txt[:300]}"
            )
        if expected_ids and len(expected_ids) <= 25:
            missing = [oid for oid in expected_ids if oid not in txt]
            if missing:
                raise RuntimeError(
                    f"Thiếu mã đơn trong Bulk Update Order ({missing}): {txt[:300]}"
                )
        return modal

    async def _wait_bulk_update_done(self, modal: Locator, timeout_ms: int = 120000) -> Tuple[str, bool]:
        """Wait up to timeout_ms for Back button, 100% completion summary, or modal auto-close after Update."""
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
        back_btn = modal.get_by_role("button", name=re.compile(r"^Back$", re.I))
        last_text = ""
        done_pat = re.compile(r"(\d+)\s+Successfully.*?(\d+)\s+Failed.*?All\s+(\d+)", re.I | re.S)
        pct_pat = re.compile(r"\b(\d+)\s*%")
        while asyncio.get_running_loop().time() < deadline:
            try:
                visible = await modal.is_visible()
            except Exception:
                visible = False
            if not visible:
                return last_text or "MODAL_CLOSED", False
            try:
                last_text = await modal.inner_text()
            except Exception:
                pass
            try:
                if await back_btn.is_visible():
                    return last_text, True
            except Exception:
                pass
            m = done_pat.search(last_text or "")
            if m:
                succ, fail, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
                pct_m = pct_pat.search(last_text or "")
                pct_ok = (pct_m is None) or (int(pct_m.group(1)) >= 100)
                if total > 0 and succ + fail >= total and pct_ok:
                    await asyncio.sleep(0.5)
                    try:
                        if await back_btn.is_visible():
                            return last_text, True
                    except Exception:
                        pass
                    try:
                        close_btn = modal.locator("button.ant-modal-close").first
                        if await close_btn.is_visible():
                            await close_btn.click()
                            await modal.wait_for(state="hidden", timeout=5000)
                    except Exception:
                        pass
                    return last_text, False
            await asyncio.sleep(0.35)
        raise TimeoutError(f"Bulk Update Order không hoàn tất trong {timeout_ms}ms: {last_text[:200]}")

    async def _submit_bulk_modal_and_cleanup(self, page: Page, modal: Locator) -> str:
        """Click Update inside modal, wait for result screen/close, close modal, clear selection, and refresh."""
        try:
            await modal.get_by_text("Bulk Update Order", exact=True).click()
            await asyncio.sleep(0.2)
        except Exception:
            pass
        await modal.get_by_role("button", name="Update", exact=True).click()
        result_text, back_visible = await self._wait_bulk_update_done(modal, timeout_ms=60000)

        if back_visible or await modal.is_visible():
            close_btn = modal.locator("button.ant-modal-close").first
            if await close_btn.is_visible():
                await close_btn.click()
                await modal.wait_for(state="hidden", timeout=5000)

        # Clear N Selected pill if visible
        sel_pill = page.locator("main").get_by_text(re.compile(r"^\d+ Selected$")).first
        if await sel_pill.count() > 0 and await sel_pill.is_visible():
            pill_close = sel_pill.locator("..").locator("span[aria-label='close'], .anticon-close, img").last
            if await pill_close.count() > 0:
                await pill_close.click()

        # Click Refresh
        refresh_btn = page.locator(
            'main button:has([aria-label="sync"]), main button:has(.anticon-sync), main button'
        ).first
        await refresh_btn.click()
        await self._wait_table_settled(page)
        return result_text

    async def _modal_add_order_tag(self, page: Page, modal: Locator, tag_name: str) -> None:
        """Select Update Order Information -> Update order(s) tag(s) -> Add order tag(s) -> ORDER STATUS / tag_name."""
        await modal.locator(".ant-select .ant-select-selector").first.click()
        cascader = page.locator(".ant-cascader-menus:visible").last
        await cascader.wait_for(state="visible", timeout=5000)
        await cascader.get_by_text("Update Order Information", exact=True).click()
        await cascader.get_by_text("Update order(s) tag(s)", exact=True).click()
        await cascader.get_by_text("Add order tag(s)", exact=True).click()

        tag_select = modal.locator(".ant-select").nth(1)
        await tag_select.wait_for(state="visible", timeout=5000)
        await tag_select.locator(".ant-select-selector").click()

        tag_menu = page.locator(".ant-cascader-menus:visible").last
        await tag_menu.wait_for(state="visible", timeout=5000)
        await tag_menu.get_by_text(TAG_GROUP, exact=True).click()
        await tag_menu.locator(".ant-cascader-menu").nth(1).get_by_text(tag_name, exact=True).click()

        # Click modal title to dismiss tag cascader without closing modal
        await modal.get_by_text("Bulk Update Order", exact=True).click()
        await asyncio.sleep(0.25)

    async def apply_step2_convert_to_call(
        self,
        target_ids: Sequence[str],
        tag_only_ids: Sequence[str] = (),
    ) -> Tuple[List[str], List[str], str]:
        """Step 2 Apply: convert Return Rate >= 50% FILL_IN orders on 3 GIPs to CALL + CALL TO CF tag."""
        if not target_ids and not tag_only_ids:
            return [], [], "0 đơn"
        assert self.page is not None
        page = self.page

        checked: List[str] = []
        skipped: List[str] = []
        svc_res = "0 đơn mới cần chuyển CALL"
        back_visible = False
        modal: Optional[Locator] = None

        if target_ids:
            await self.scan_step2_orders()
            checked, skipped = await self._select_exact_ids(
                page, set(target_ids), require_unassigned=False, require_fillin=True
            )
            if checked:
                modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
                # 1. Update FFM service type -> Update (Overwrite) -> CALL
                await modal.locator(".ant-select .ant-select-selector").first.click()
                cascader = page.locator(".ant-cascader-menus:visible").last
                await cascader.wait_for(state="visible", timeout=5000)
                await cascader.locator(".ant-cascader-menu-item").filter(has_text="Update FFM service type").first.click()
                await modal.locator(".ant-select .ant-select-selector").nth(1).click()
                await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(
                    has_text=re.compile(r"Update\s*\(Overwrite\)", re.I)
                ).first.click()
                svc_input = modal.locator(".ant-select").nth(2).locator("input").first
                await svc_input.fill("CALL")
                await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(
                    has_text=re.compile(r"^CALL$", re.I)
                ).first.click()
                await modal.get_by_text("Bulk Update Order", exact=True).click()
                await asyncio.sleep(0.2)
                await modal.get_by_role("button", name="Update", exact=True).click()
                svc_res, back_visible = await self._wait_bulk_update_done(modal, timeout_ms=60000)

        tag_targets = list(dict.fromkeys([*checked, *tag_only_ids]))
        if not tag_targets:
            return [], skipped, "0 đơn còn FILL_IN/New"

        # 2. Add tag ORDER STATUS / CALL TO CF (via Back if only checked items, else re-select all needing tag)
        if back_visible and modal is not None and not tag_only_ids:
            await modal.get_by_role("button", name="Back", exact=True).click()
            await modal.locator(".ant-select .ant-select-selector").first.wait_for(state="visible", timeout=5000)
            await asyncio.sleep(0.35)
        else:
            if back_visible and modal is not None:
                try:
                    close_btn = modal.locator("button.ant-modal-close").first
                    if await close_btn.is_visible():
                        await close_btn.click()
                        await modal.wait_for(state="hidden", timeout=5000)
                except Exception:
                    pass
            await self.scan_step2_orders()
            rechecked, tag_skipped = await self._select_exact_ids(
                page, set(tag_targets), require_unassigned=False, require_fillin=False
            )
            if not rechecked:
                return checked, [*skipped, *tag_skipped], f"Service: {svc_res[:120]} | Tag: 0 đơn còn New"
            modal = await self._open_bulk_modal_and_verify(page, len(rechecked), rechecked)
        await self._modal_add_order_tag(page, modal, "CALL TO CF")
        tag_res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return checked or tag_targets, skipped, f"Service: {svc_res[:120]} | Tag: {tag_res[:120]}"

    async def apply_bulk_tag_and_cancel(
        self,
        target_ids: Sequence[str],
        tag_name: str,
        cancel_reason_pattern: str,
    ) -> Tuple[List[str], List[str], str]:
        """On Telesale FILL IN DATA New, add ORDER STATUS / tag_name and cancel with reason."""
        if not target_ids:
            return [], [], "0 đơn"
        assert self.page is not None
        page = self.page
        await self._prepare_telesale_fill_in_new(page)
        checked, skipped = await self._select_exact_ids(page, set(target_ids), require_unassigned=True)
        if not checked:
            return [], skipped, "0 đơn còn New/chưa chia"

        modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
        # 1. Add tag
        await self._modal_add_order_tag(page, modal, tag_name)
        pre_tag = await modal.inner_text()
        if f"{TAG_GROUP} / {tag_name}" not in pre_tag:
            raise RuntimeError(f"Xác minh tag thất bại trước khi bấm Update: {pre_tag[:300]}")
        await modal.get_by_role("button", name="Update", exact=True).click()
        tag_res, back_visible = await self._wait_bulk_update_done(modal, timeout_ms=60000)

        # 2. Back (or re-open bulk modal if auto-closed) -> Cancel with reason
        if back_visible:
            await modal.get_by_role("button", name="Back", exact=True).click()
            await modal.locator(".ant-select .ant-select-selector").first.wait_for(state="visible", timeout=5000)
            await asyncio.sleep(0.35)
        else:
            await self._prepare_telesale_fill_in_new(page)
            rechecked, _ = await self._select_exact_ids(page, set(checked), require_unassigned=True)
            modal = await self._open_bulk_modal_and_verify(page, len(rechecked), rechecked)
        await modal.locator(".ant-select .ant-select-selector").first.click()
        cascader = page.locator(".ant-cascader-menus:visible").last
        await cascader.wait_for(state="visible", timeout=5000)
        await cascader.locator(".ant-cascader-menu").first.get_by_text("Update order(s) status", exact=True).click()

        status_select = modal.locator(".ant-select").nth(1)
        await status_select.wait_for(state="visible", timeout=5000)
        await status_select.locator(".ant-select-selector").click()
        await status_select.locator("input").first.fill("Canceled")
        await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(
            has_text=re.compile(r"^Canceled$")
        ).first.click()

        reason_select = modal.locator(".ant-select").nth(2)
        await reason_select.wait_for(state="visible", timeout=5000)
        await reason_select.locator(".ant-select-selector").click()
        reason_input = reason_select.locator("input").first
        search_hint = re.sub(r"\\b|\^|\$", "", cancel_reason_pattern).strip()
        await reason_input.fill(search_hint)

        reason_opt = page.locator(".ant-select-dropdown:visible").last.locator(".ant-select-item-option").filter(
            has_text=re.compile(re.sub(r"\\b$", "", cancel_reason_pattern), re.I)
        ).first
        await reason_opt.wait_for(state="visible", timeout=5000)
        await reason_opt.click()

        cancel_res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return checked, skipped, f"Tag: {tag_res[:100]} | Cancel: {cancel_res[:100]}"

    async def apply_bulk_tag_only(
        self,
        target_ids: Sequence[str],
        tag_name: str,
    ) -> Tuple[List[str], List[str], str]:
        """Add ORDER STATUS / tag_name without canceling (for DIFFERENT PRODUCT / RE ORDER)."""
        if not target_ids:
            return [], [], "0 đơn"
        assert self.page is not None
        page = self.page
        await self._prepare_telesale_fill_in_new(page)
        checked, skipped = await self._select_exact_ids(page, set(target_ids), require_unassigned=True)
        if not checked:
            return [], skipped, "0 đơn còn New/chưa chia"

        modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
        await self._modal_add_order_tag(page, modal, tag_name)
        res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return checked, skipped, res[:140]

    async def apply_bulk_assign_sale(
        self,
        target_ids: Sequence[str],
        sale_accounts: Sequence[str],
    ) -> Tuple[List[str], List[str], str]:
        """Assign target_ids to active sale_accounts via Bulk Update Order."""
        if not target_ids or not sale_accounts:
            return [], list(target_ids), "0 đơn hoặc không có sale đi làm"
        assert self.page is not None
        page = self.page
        await self._prepare_telesale_fill_in_new(page)
        checked, skipped = await self._select_exact_ids(page, set(target_ids), require_unassigned=True)
        if not checked:
            return [], skipped, "0 đơn còn New/chưa chia"

        modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
        await modal.locator(".ant-select .ant-select-selector").first.click()
        cascader = page.locator(".ant-cascader-menus:visible").last
        await cascader.wait_for(state="visible", timeout=5000)
        await cascader.locator(".ant-cascader-menu").first.get_by_text("Assign/Unassign sale", exact=True).click()

        col2 = cascader.locator(".ant-cascader-menu").nth(1)
        await col2.wait_for(state="visible", timeout=5000)
        await col2.locator(".ant-cascader-menu-item").filter(
            has_text=re.compile(r"^\s*(\+\s*)?Assign sale\s*$", re.I)
        ).first.click()

        sale_select = modal.locator(".ant-select").nth(1)
        await sale_select.wait_for(state="visible", timeout=5000)
        await sale_select.locator(".ant-select-selector").click()
        sale_input = sale_select.locator("input").first

        for acct in sale_accounts:
            await sale_input.fill(acct)
            dropdown = page.locator(".ant-select-dropdown:visible").last
            opt = dropdown.locator(".ant-select-item-option").filter(has_text=acct).first
            await opt.wait_for(state="visible", timeout=5000)
            await opt.click()

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
        res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return checked, skipped, res[:160]

    # ------------------------------------------------------------------
    # GIP 287 (WeUpBook) Navigation, Tooltip Scraping & Mutations
    # ------------------------------------------------------------------
    async def _select_pos_shop_filter(self, page: Page, query: str, exact_label: str) -> None:
        popup = await self._open_add_filter_popup(page)
        await popup.get_by_text("POS Shop", exact=True).first.click()
        search_input = popup.get_by_placeholder("Search POS Shop")
        await search_input.wait_for(state="visible", timeout=5000)
        await search_input.fill(query)
        opt = popup.get_by_text(re.compile(re.escape(exact_label), re.I)).first
        await opt.wait_for(state="visible", timeout=5000)
        await opt.click()
        await self._close_popup(page)

    async def _prepare_order_gip287_new(self, page: Page) -> Dict[str, int]:
        """Open /admin/order filtered to POS Shop = 408023150 - GIP 287 on tab New with Return Rate."""
        await page.goto(GSOLUTION_ORDER_URL, wait_until="domcontentloaded")
        await page.locator("main").first.wait_for(state="visible", timeout=15000)
        await self._clear_all_filters(page)
        await self._select_pos_shop_filter(page, GIP287_SHOP_QUERY, GIP287_SHOP_LABEL)
        tab_counts = await self._select_tab(page, "New")
        await self._ensure_return_rate_column(page)
        await self._set_page_size_1000(page)
        return tab_counts

    async def _read_return_rate_tooltip_for_order(
        self, page: Page, order_id: str
    ) -> Tuple[Optional[int], Optional[int]]:
        """Hover the Return Rate cell for order_id and parse (success, returned) from tooltip."""
        headers = [h.strip() for h in await page.locator("main table th").all_inner_texts()]
        if "Return Rate" not in headers or "ID" not in headers:
            return None, None
        rr_idx = headers.index("Return Rate")
        id_idx = headers.index("ID")

        await page.evaluate(
            "() => { const h = document.querySelector('.ant-table-tbody-virtual-holder'); if (h) h.scrollTop = 0; }"
        )
        await asyncio.sleep(0.15)

        pos: Optional[Dict[str, object]] = None
        for _ in range(120):
            found = await page.evaluate(
                """({ oid, idIdx, rrIdx }) => {
                    const cbs = Array.from(document.querySelectorAll('main input[type="checkbox"]')).slice(1);
                    for (const cb of cbs) {
                        let row = cb;
                        for (let d = 0; d < 7; d++) {
                            if (!row) break;
                            row = row.parentElement;
                            const divs = Array.from(row.children).filter(c => c.tagName === 'DIV');
                            if (divs.length >= Math.max(idIdx, rrIdx) + 1) {
                                const cellId = (divs[idIdx]?.innerText || '').trim();
                                if (cellId === oid) {
                                    const rrCell = divs[rrIdx];
                                    const target = rrCell.querySelector('.ant-tag, span, div') || rrCell;
                                    target.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                                    const h = document.querySelector('.ant-table-tbody-virtual-holder');
                                    if (h) {
                                        const rCheck = target.getBoundingClientRect();
                                        if (rCheck.right > window.innerWidth - 20 || rCheck.left < 20) {
                                            h.scrollLeft = Math.max(0, rrCell.offsetLeft - Math.floor(h.clientWidth / 2));
                                        }
                                    }
                                    return { found: true };
                                }
                                break;
                            }
                        }
                    }
                    const h = document.querySelector('.ant-table-tbody-virtual-holder');
                    let atBottom = true;
                    if (h) {
                        atBottom = (h.scrollTop + h.clientHeight >= h.scrollHeight - 2);
                        if (!atBottom) {
                            const prev = h.scrollTop;
                            h.scrollTop = Math.min(h.scrollHeight, prev + Math.max(200, Math.floor(h.clientHeight * 0.6)));
                            if (h.scrollTop === prev) atBottom = true;
                        }
                    }
                    return { found: false, atBottom };
                }""",
                {"oid": order_id, "idIdx": id_idx, "rrIdx": rr_idx},
            )
            if found["found"]:
                await asyncio.sleep(0.18)
                pos = await page.evaluate(
                    """({ oid, idIdx, rrIdx }) => {
                        const cbs = Array.from(document.querySelectorAll('main input[type="checkbox"]')).slice(1);
                        for (const cb of cbs) {
                            let row = cb;
                            for (let d = 0; d < 7; d++) {
                                if (!row) break;
                                row = row.parentElement;
                                const divs = Array.from(row.children).filter(c => c.tagName === 'DIV');
                                if (divs.length >= Math.max(idIdx, rrIdx) + 1) {
                                    const cellId = (divs[idIdx]?.innerText || '').trim();
                                    if (cellId === oid) {
                                        const rrCell = divs[rrIdx];
                                        const target = rrCell.querySelector('.ant-tag, span, div') || rrCell;
                                        const r = target.getBoundingClientRect();
                                        const x = r.left + r.width / 2;
                                        const y = r.top + r.height / 2;
                                        if (r.width > 0 && r.height > 0 && x >= 0 && x <= window.innerWidth && y >= 0 && y <= window.innerHeight) {
                                            return { x, y, tipId: target.getAttribute('aria-describedby') || '' };
                                        }
                                    }
                                    break;
                                }
                            }
                        }
                        return null;
                    }""",
                    {"oid": order_id, "idIdx": id_idx, "rrIdx": rr_idx},
                )
                break
            if found["atBottom"]:
                break
            await asyncio.sleep(0.12)

        if pos is None:
            return None, None

        # Clear any stale tooltip first, then hover target badge/cell inside viewport
        await page.mouse.move(220, 18)
        await asyncio.sleep(0.15)
        await page.mouse.move(float(pos["x"]), float(pos["y"]))

        succ: Optional[int] = None
        ret: Optional[int] = None
        tip_id = str(pos.get("tipId") or "")
        deadline = asyncio.get_running_loop().time() + 2.5
        while asyncio.get_running_loop().time() < deadline:
            if tip_id:
                try:
                    by_id_txt = await page.evaluate(
                        "(id) => document.getElementById(id)?.innerText || ''", tip_id
                    )
                    s_val, r_val = parse_return_rate_tooltip(by_id_txt)
                    if s_val is not None and r_val is not None:
                        succ, ret = s_val, r_val
                        break
                except Exception:
                    pass
            tips = await page.locator(".ant-tooltip:visible, [role='tooltip']:visible").all_inner_texts()
            for txt in tips:
                s_val, r_val = parse_return_rate_tooltip(txt)
                if s_val is not None and r_val is not None:
                    succ, ret = s_val, r_val
                    break
            if succ is not None:
                break
            await asyncio.sleep(0.12)

        await page.mouse.move(220, 18)
        await asyncio.sleep(0.1)
        return succ, ret

    async def scan_gip287_phase1_orders(self) -> Tuple[int, List[OrderRecord]]:
        """Phase 1: Scrape GIP 287 New orders on /admin/order and hover Return Rate tooltips for >= 60%."""
        assert self.page is not None
        page = self.page
        tab_counts = await self._prepare_order_gip287_new(page)
        total_new = _normalize_tab_key(tab_counts, "New")
        rows = await self._scrape_all_pages_virtual_table(page, expected_total=total_new)

        for o in rows:
            already_tagged = any(
                t.strip().upper() in {"NEED PAY ADVANCED", "NEED PAY IN ADVANCE"}
                for t in o.tags
            )
            if already_tagged:
                continue
            if o.return_rate is not None and o.return_rate >= GIP287_RETURN_RATE_THRESHOLD:
                succ, ret = await self._read_return_rate_tooltip_for_order(page, o.id)
                o.return_rate_success = succ
                o.return_rate_returned = ret

        await page.evaluate(
            "() => { const h = document.querySelector('.ant-table-tbody-virtual-holder'); if (h) h.scrollLeft = 0; }"
        )
        return total_new, rows

    async def apply_gip287_phase1_tag(
        self,
        target_ids: Sequence[str],
        tag_name: str = TAG_NEED_PAY_ADVANCED,
    ) -> Tuple[List[str], List[str], str]:
        """Phase 1 Apply: Add ORDER STATUS / NEED PAY ADVANCED on /admin/order (keep Service Type)."""
        if not target_ids:
            return [], [], "0 đơn"
        assert self.page is not None
        page = self.page
        await self._prepare_order_gip287_new(page)
        checked, skipped = await self._select_exact_ids(
            page, set(target_ids), require_unassigned=False, require_fillin=False, allowed_statuses=("New",)
        )
        if not checked:
            return [], skipped, "0 đơn còn New"

        modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
        await self._modal_add_order_tag(page, modal, tag_name)
        pre_tag = await modal.inner_text()
        if f"{TAG_GROUP} / {tag_name}" not in pre_tag:
            raise RuntimeError(f"Xác minh tag thất bại trước khi bấm Update: {pre_tag[:300]}")
        res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return checked, skipped, res[:140]

    async def apply_gip287_phase1_cancel_test(
        self,
        target_ids: Sequence[str],
        tag_name: str = TAG_TEST,
        cancel_reason_pattern: str = r"^Test",
    ) -> Tuple[List[str], List[str], str]:
        """Phase 1 Apply: Tag TEST and cancel matching New GIP 287 orders on /admin/order."""
        if not target_ids:
            return [], [], "0 đơn"
        assert self.page is not None
        page = self.page
        await self._prepare_order_gip287_new(page)
        checked, skipped = await self._select_exact_ids(
            page, set(target_ids), require_unassigned=False, require_fillin=False, allowed_statuses=("New",)
        )
        if not checked:
            return [], skipped, "0 đơn còn New"
        tag_res, cancel_res = await self._modal_tag_and_cancel(
            page,
            checked,
            tag_name,
            cancel_reason_pattern,
            self._prepare_order_gip287_new,
            require_unassigned=False,
            require_fillin=False,
            allowed_statuses=("New",),
        )
        return checked, skipped, f"Tag: {tag_res[:100]} | Cancel: {cancel_res[:100]}"

    async def _prepare_telesale_gip287_dup_new(self, page: Page) -> Dict[str, int]:
        """Open /admin/order/telesale filtered to POS Shop GIP 287 + Duplicated Phone on tab New."""
        await page.goto(GSOLUTION_TELESALE_URL, wait_until="domcontentloaded")
        await page.locator("main").first.wait_for(state="visible", timeout=15000)
        await self._clear_all_filters(page)
        await self._select_pos_shop_filter(page, GIP287_SHOP_QUERY, GIP287_SHOP_LABEL)

        popup = await self._open_add_filter_popup(page)
        await popup.get_by_text("Duplicated Phone", exact=True).first.click()
        dup_opts = popup.get_by_text("Duplicated Phone", exact=True)
        cnt = await dup_opts.count()
        await (dup_opts.nth(1) if cnt >= 2 else dup_opts.first).click()
        await self._close_popup(page)

        tabs = await self._select_tab(page, "New")
        await self._set_page_size_1000(page)
        return tabs

    async def scan_gip287_telesale_dup_new(self) -> Tuple[int, List[OrderRecord]]:
        """Phase 2: Scrape GIP 287 Duplicated Phone New orders on /admin/order/telesale."""
        assert self.page is not None
        page = self.page
        tabs = await self._prepare_telesale_gip287_dup_new(page)
        total_new = _normalize_tab_key(tabs, "New")
        rows = await self._scrape_all_pages_virtual_table(page, expected_total=total_new)
        return total_new, rows

    async def inspect_order_detail_sku(self, order_id: str) -> str:
        """Open /admin/order?keyword=<order_id> modal strictly read-only to read full SKU/Note text."""
        assert self.context is not None
        pg = await self.context.new_page()
        pg.set_default_timeout(15000)
        try:
            url = f"{GSOLUTION_ORDER_URL}?keyword={urllib.parse.quote(order_id, safe='')}"
            await pg.goto(url, wait_until="domcontentloaded")
            await pg.locator('[role="tab"]').first.wait_for(state="visible", timeout=15000)
            await self._wait_table_settled(pg)
            for _ in range(20):
                if await pg.locator('main input[type="checkbox"]').count() > 1:
                    break
                await asyncio.sleep(0.2)
            holder = pg.locator(".ant-table-tbody-virtual-holder")
            if await holder.count() > 0:
                row = holder.locator(".ant-table-row").filter(has_text=order_id).first
            else:
                row = pg.locator("main .ant-table-row").filter(has_text=order_id).first
            await row.locator("> div").nth(2).click(timeout=5000)

            modal = pg.locator(".ant-modal-content:visible").last
            await modal.wait_for(state="visible", timeout=8000)
            await asyncio.sleep(0.3)
            modal_text = await modal.inner_text()

            close_btn = modal.locator("button.ant-modal-close").first
            if await close_btn.is_visible():
                await close_btn.click()
            await modal.wait_for(state="hidden", timeout=5000)
            return modal_text
        except Exception as exc:
            LOGGER.warning("Không đọc được chi tiết mã SP đơn %s: %s", order_id, exc)
            return ""
        finally:
            await pg.close()

    async def apply_gip287_tag_only(
        self,
        target_ids: Sequence[str],
        tag_name: str,
    ) -> Tuple[List[str], List[str], str]:
        """Phase 2 Apply: Add ORDER STATUS / tag_name on Telesale GIP 287 Duplicated Phone New."""
        if not target_ids:
            return [], [], "0 đơn"
        assert self.page is not None
        page = self.page
        await self._prepare_telesale_gip287_dup_new(page)
        checked, skipped = await self._select_exact_ids(
            page, set(target_ids), require_unassigned=False, require_fillin=False, allowed_statuses=("New",)
        )
        if not checked:
            return [], skipped, "0 đơn còn New"

        modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
        await self._modal_add_order_tag(page, modal, tag_name)
        pre_tag = await modal.inner_text()
        if f"{TAG_GROUP} / {tag_name}" not in pre_tag:
            raise RuntimeError(f"Xác minh tag thất bại trước khi bấm Update: {pre_tag[:300]}")
        res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return checked, skipped, res[:140]

    async def _modal_tag_and_cancel(
        self,
        page: Page,
        checked: List[str],
        tag_name: str,
        cancel_reason_pattern: str,
        reprepare_fn: Callable[[Page], object],
        require_unassigned: bool,
        require_fillin: bool,
        allowed_statuses: Sequence[str],
    ) -> Tuple[str, str]:
        modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
        await self._modal_add_order_tag(page, modal, tag_name)
        pre_tag = await modal.inner_text()
        if f"{TAG_GROUP} / {tag_name}" not in pre_tag:
            raise RuntimeError(f"Xác minh tag thất bại trước khi bấm Update: {pre_tag[:300]}")
        await modal.get_by_role("button", name="Update", exact=True).click()
        tag_res, back_visible = await self._wait_bulk_update_done(modal, timeout_ms=60000)

        if back_visible:
            await modal.get_by_role("button", name="Back", exact=True).click()
            await modal.locator(".ant-select .ant-select-selector").first.wait_for(state="visible", timeout=5000)
            await asyncio.sleep(0.35)
        else:
            await reprepare_fn(page)
            rechecked, _ = await self._select_exact_ids(
                page,
                set(checked),
                require_unassigned=require_unassigned,
                require_fillin=require_fillin,
                allowed_statuses=allowed_statuses,
            )
            modal = await self._open_bulk_modal_and_verify(page, len(rechecked), rechecked)

        await modal.locator(".ant-select .ant-select-selector").first.click()
        cascader = page.locator(".ant-cascader-menus:visible").last
        await cascader.wait_for(state="visible", timeout=5000)
        await cascader.locator(".ant-cascader-menu").first.get_by_text("Update order(s) status", exact=True).click()

        status_select = modal.locator(".ant-select").nth(1)
        await status_select.wait_for(state="visible", timeout=5000)
        await status_select.locator(".ant-select-selector").click()
        await status_select.locator("input").first.fill("Canceled")
        await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(
            has_text=re.compile(r"^Canceled$")
        ).first.click()

        reason_select = modal.locator(".ant-select").nth(2)
        await reason_select.wait_for(state="visible", timeout=5000)
        await reason_select.locator(".ant-select-selector").click()
        reason_input = reason_select.locator("input").first
        search_hint = re.sub(r"\\b|\^|\$", "", cancel_reason_pattern).strip()
        await reason_input.fill(search_hint)

        reason_opt = page.locator(".ant-select-dropdown:visible").last.locator(".ant-select-item-option").filter(
            has_text=re.compile(re.sub(r"\\b$", "", cancel_reason_pattern), re.I)
        ).first
        await reason_opt.wait_for(state="visible", timeout=5000)
        await reason_opt.click()

        cancel_res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return tag_res, cancel_res

    async def apply_gip287_cancel_new(
        self,
        target_ids: Sequence[str],
        tag_name: str,
        cancel_reason_pattern: str,
    ) -> Tuple[List[str], List[str], str]:
        """Phase 2 Apply: Tag and cancel GIP 287 New orders on Telesale Duplicated Phone."""
        if not target_ids:
            return [], [], "0 đơn"
        assert self.page is not None
        page = self.page
        await self._prepare_telesale_gip287_dup_new(page)
        checked, skipped = await self._select_exact_ids(
            page, set(target_ids), require_unassigned=False, require_fillin=False, allowed_statuses=("New",)
        )
        if not checked:
            return [], skipped, "0 đơn còn New"
        tag_res, cancel_res = await self._modal_tag_and_cancel(
            page,
            checked,
            tag_name,
            cancel_reason_pattern,
            self._prepare_telesale_gip287_dup_new,
            require_unassigned=False,
            require_fillin=False,
            allowed_statuses=("New",),
        )
        return checked, skipped, f"Tag: {tag_res[:100]} | Cancel: {cancel_res[:100]}"

    async def apply_gip287_cancel_peer_order(
        self,
        order_id: str,
        tag_name: str = TAG_DUPLICATE,
        cancel_reason_pattern: str = r"^Duplicate",
    ) -> Tuple[List[str], List[str], str]:
        """Safely tag DUPLICATE and cancel a smaller subset order up to Pending status on /admin/order."""
        assert self.page is not None
        page = self.page
        allowed = tuple(GIP287_CANCELABLE_SMALL_STATUSES)

        async def _open_peer(pg: Page) -> None:
            url = f"{GSOLUTION_ORDER_URL}?keyword={urllib.parse.quote(order_id, safe='')}"
            await pg.goto(url, wait_until="domcontentloaded")
            await pg.locator('[role="tab"]').first.wait_for(state="visible", timeout=15000)
            await self._wait_table_settled(pg)
            await self._set_page_size_1000(pg)

        await _open_peer(page)
        checked, skipped = await self._select_exact_ids(
            page, {order_id}, require_unassigned=False, require_fillin=False, allowed_statuses=allowed
        )
        if not checked:
            return [], [order_id], "Bỏ qua: đơn không còn ở trạng thái <= Pending"

        tag_res, cancel_res = await self._modal_tag_and_cancel(
            page,
            checked,
            tag_name,
            cancel_reason_pattern,
            _open_peer,
            require_unassigned=False,
            require_fillin=False,
            allowed_statuses=allowed,
        )
        return checked, skipped, f"Tag: {tag_res[:100]} | Cancel: {cancel_res[:100]}"

    async def apply_gip287_tag_peer_order(
        self,
        order_id: str,
        tag_name: str = TAG_DUPLICATE,
    ) -> Tuple[List[str], List[str], str]:
        """Tag-only (--no-cancel): add ORDER STATUS / tag_name on a smaller subset peer up to Pending."""
        assert self.page is not None
        page = self.page
        allowed = tuple(GIP287_CANCELABLE_SMALL_STATUSES)

        url = f"{GSOLUTION_ORDER_URL}?keyword={urllib.parse.quote(order_id, safe='')}"
        await page.goto(url, wait_until="domcontentloaded")
        await page.locator('[role="tab"]').first.wait_for(state="visible", timeout=15000)
        await self._wait_table_settled(page)
        await self._set_page_size_1000(page)

        checked, skipped = await self._select_exact_ids(
            page, {order_id}, require_unassigned=False, require_fillin=False, allowed_statuses=allowed
        )
        if not checked:
            return [], [order_id], "Bỏ qua: đơn không còn ở trạng thái <= Pending"

        modal = await self._open_bulk_modal_and_verify(page, len(checked), checked)
        await self._modal_add_order_tag(page, modal, tag_name)
        pre_tag = await modal.inner_text()
        if f"{TAG_GROUP} / {tag_name}" not in pre_tag:
            raise RuntimeError(f"Xác minh tag thất bại trước khi bấm Update: {pre_tag[:300]}")
        res = await self._submit_bulk_modal_and_cleanup(page, modal)
        return checked, skipped, res[:140]
