"""Rule engine for Step 2, Step 3A (Duplicated Phone), and Step 3B (Not Duplicated Phone)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from config import (
    ACTIVE_STATUSES,
    DEFERRED_FILL_GIPS,
    DUPLICATE_WINDOW_DAYS,
    EMPTY_ORDER_COOLDOWN_MINUTES,
    FINAL_STATUSES,
    GIP287_CANCELABLE_SMALL_STATUSES,
    GIP287_DUPLICATE_WINDOW_DAYS,
    GIP287_FINAL_STATUSES,
    GIP287_GIP_KEY,
    GIP287_MIN_RETURNED_COUNT,
    GIP287_RETURN_RATE_THRESHOLD,
    MULTIPLE_RETURNED_THRESHOLD,
    STEP2_RETURN_RATE_THRESHOLD,
    TAG_CALL_TO_CF,
    TAG_DIFFERENT_PRODUCT,
    TAG_DUPLICATE,
    TAG_MULTIPLE_ORDERS,
    TAG_MULTIPLE_RETURNED,
    TAG_NEED_PAY_ADVANCED,
    TAG_RE_ORDER,
    TAG_TEST,
)

# Color/size tokens stripped from product names so variants of the same product match
_COLOR_WORDS = (
    "charcoal blue",
    "dark blue",
    "light blue",
    "navy blue",
    "skin color",
    "skin tone",
    "sky blue",
    "black",
    "white",
    "gray",
    "grey",
    "blue",
    "red",
    "green",
    "yellow",
    "orange",
    "pink",
    "purple",
    "brown",
    "beige",
    "kaki",
    "khaki",
)

_GIP_CODE_RE = re.compile(r"\bGIP\s*(\d+)\b", re.IGNORECASE)
_SKU_RE = re.compile(r"\bGPH\d+-\d+\b", re.IGNORECASE)
_ANGLE_PRODUCT_RE = re.compile(r"<([^<>]{2,100})>")
_URL_SLUG_RE = re.compile(r"https?://[^\s/]+/([a-z0-9][a-z0-9_-]{2,60})(?:[/?#\s]|$)", re.IGNORECASE)
_GENERIC_URL_SLUGS = {
    "index",
    "home",
    "shop",
    "store",
    "product",
    "products",
    "order",
    "checkout",
    "cart",
    "page",
    "pages",
    "collections",
}
_QTY_SUFFIX_RE = re.compile(r"\s+x\s*\d+\s*$", re.IGNORECASE)
_SIZE_RE = re.compile(
    r"\bsize\s*(?:\d*x*[sml]|free\s*size|\d+(?:-\d+)?)\b", re.IGNORECASE
)
_COLOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(c) for c in _COLOR_WORDS) + r")\b",
    re.IGNORECASE,
)
_CODE_SUFFIX_RE = re.compile(r"\s*-\s*D\d+\b", re.IGNORECASE)
_DEFINITE_TEST_RE = re.compile(r"\btest\b", re.IGNORECASE)
_STRIP_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class OrderRecord:
    """Normalized order row scraped from G-solution."""

    id: str
    shop: str = ""
    service_types: str = ""
    tags: List[str] = field(default_factory=list)
    assigning_sale: str = ""
    customer_name: str = ""
    phone: str = ""
    address: str = ""
    note: str = ""
    internal_note: str = ""
    note_print: str = ""
    products: str = ""
    created_at: str = ""
    updated_at: str = ""
    status: str = ""
    return_rate: Optional[float] = None
    return_rate_success: Optional[int] = None
    return_rate_returned: Optional[int] = None
    detail_sku_code: str = ""
    empty_detail_confirmed: Optional[bool] = None

    @property
    def normalized_phone(self) -> str:
        return normalize_phone(self.phone)

    @property
    def gip_key(self) -> str:
        return extract_gip_key(self.shop)


@dataclass
class PhoneHistory:
    """Full order history and tab counts for a normalized phone number."""

    normalized_phone: str
    all_count: int = 0
    returned_count: int = 0
    tab_counts: Dict[str, int] = field(default_factory=dict)
    orders: List[OrderRecord] = field(default_factory=list)
    has_test_hit: bool = False
    incomplete_scan: bool = False


@dataclass
class ClassificationResult:
    """Decision produced for a single order."""

    order: OrderRecord
    bucket: str  # TEST | DUPLICATE | MULTIPLE RETURNED | DIFFERENT PRODUCT | RE ORDER | ASSIGN_ONLY | AMBIGUOUS
    action: str  # CANCEL | ASSIGN | REVIEW | CONVERT_TO_CALL
    tag: str = ""
    cancel_reason_pattern: str = ""
    reason: str = ""
    reference_ids: List[str] = field(default_factory=list)
    returned_count: int = 0
    applied_status: str = "PENDING"  # PENDING | APPLIED | SKIPPED | FAILED | DRY_RUN


def normalize_phone(phone: str) -> str:
    """Normalize PH phone strings (+639..., 639..., 9...) into 09xxxxxxxxx."""
    digits = re.sub(r"[^\d]", "", (phone or "").strip())
    if digits.startswith("63") and len(digits) >= 12:
        return "0" + digits[2:]
    if digits.startswith("9") and len(digits) == 10:
        return "0" + digits
    return digits


def phone_search_variants(phone: str) -> List[str]:
    """Return canonical query variants [09..., +639...] for G-solution keyword search."""
    raw = (phone or "").strip()
    norm = normalize_phone(raw)
    variants: List[str] = []
    if norm:
        variants.append(norm)
    if norm.startswith("09") and len(norm) >= 11:
        intl = "+63" + norm[1:]
        if intl not in variants:
            variants.append(intl)
    elif raw and raw not in variants:
        variants.append(raw)
    return variants


def extract_gip_key(shop: str) -> str:
    """Extract canonical GIP shop key (e.g., 'GIP 264') for same-GIP comparison."""
    s = (shop or "").strip()
    m = _GIP_CODE_RE.search(s)
    if m:
        return f"GIP {m.group(1)}"
    return s.lower()


def parse_return_rate(text: str) -> Optional[float]:
    """Parse percentage value from Return Rate cell (e.g., '50 %' -> 50.0)."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    return float(m.group(1)) if m else None


def parse_created_at(text: str, ref_date: datetime) -> Optional[datetime]:
    """Parse G-solution Created At string relative to ref_date."""
    s = (text or "").strip()
    if not s:
        return None

    m_rel = re.match(r"^(\d{2}):(\d{2}):(\d{2})\s+(Today|Yesterday)$", s, re.IGNORECASE)
    if m_rel:
        hh, mm, ss = int(m_rel.group(1)), int(m_rel.group(2)), int(m_rel.group(3))
        base = ref_date.date()
        if m_rel.group(4).lower() == "yesterday":
            base = base - timedelta(days=1)
        return datetime(base.year, base.month, base.day, hh, mm, ss, tzinfo=ref_date.tzinfo)

    m_date = re.match(r"^(\d{2}):(\d{2}):(\d{2})\s+(\d{2})/(\d{2})(?:/(\d{4}))?$", s)
    if m_date:
        hh, mm, ss = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        day, month = int(m_date.group(4)), int(m_date.group(5))
        if m_date.group(6):
            year = int(m_date.group(6))
        else:
            year = ref_date.year
            if (month, day) > (ref_date.month, ref_date.day):
                year -= 1
        try:
            return datetime(year, month, day, hh, mm, ss, tzinfo=ref_date.tzinfo)
        except ValueError:
            return None

    return None


def is_within_days(created_dt: Optional[datetime], ref_date: datetime, days: int = DUPLICATE_WINDOW_DAYS) -> bool:
    """Return True if created_dt falls within the last `days` calendar days (inclusive)."""
    if created_dt is None:
        return False
    cutoff_date = ref_date.date() - timedelta(days=days)
    return created_dt.date() >= cutoff_date


def is_order_younger_than(
    order: OrderRecord,
    ref_date: Optional[datetime],
    minutes: int = EMPTY_ORDER_COOLDOWN_MINUTES,
) -> bool:
    """Return True if order was created less than `minutes` before ref_date."""
    if ref_date is None or minutes <= 0:
        return False
    created_dt = parse_created_at(order.created_at, ref_date)
    if created_dt is None:
        return False
    age_seconds = (ref_date - created_dt).total_seconds()
    return 0 <= age_seconds < minutes * 60


def detect_test_text(texts: Iterable[str]) -> Tuple[str, str]:
    """Check strings for standalone 'test' keyword -> ('DEFINITE'|'NONE', matched_snippet)."""
    for raw in texts:
        s = _STRIP_URL_RE.sub(" ", (raw or "")).strip()
        if not s:
            continue
        m_def = _DEFINITE_TEST_RE.search(s)
        if m_def:
            return "DEFINITE", f"{m_def.group(0)} in '{s[:80]}'"
    return "NONE", ""


def has_test_order_flag(order: OrderRecord) -> Tuple[str, str]:
    """Check whether a single order has an explicit TEST tag or test text."""
    for tag in order.tags:
        if tag.strip().upper() == "TEST":
            return "DEFINITE", "Tag TEST"
    return detect_test_text(
        [
            order.customer_name,
            order.address,
            order.note,
            order.internal_note,
            order.note_print,
        ]
    )


def _normalize_product_name(name: str) -> str:
    """Normalize a product name by removing quantity, size, and color variant tokens."""
    s = name.lower().strip()
    s = _QTY_SUFFIX_RE.sub("", s)
    s = _CODE_SUFFIX_RE.sub("", s)
    s = _SIZE_RE.sub("", s)
    s = _COLOR_RE.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_product_keys(internal_note: str, products: str) -> Tuple[Set[str], Set[str]]:
    """Return (normalized_name_keys, sku_keys) from Internal Note and Products."""
    name_keys: Set[str] = set()
    sku_keys: Set[str] = set()

    inote = (internal_note or "").strip()
    prod = (products or "").strip()

    for sku in _SKU_RE.findall(inote):
        sku_keys.add(sku.upper())

    for match in _ANGLE_PRODUCT_RE.findall(inote):
        norm = _normalize_product_name(match)
        if norm and norm not in {"note", "address", "promo"}:
            name_keys.add(norm)

    for slug in _URL_SLUG_RE.findall(inote):
        if slug.lower() in _GENERIC_URL_SLUGS:
            continue
        norm = _normalize_product_name(slug.replace("-", " ").replace("_", " "))
        if norm and len(norm) >= 4:
            name_keys.add(norm)

    if prod and prod not in {"-", "Empty Cart"}:
        for part in prod.split(","):
            norm = _normalize_product_name(part)
            if norm:
                name_keys.add(norm)

    return name_keys, sku_keys


def is_empty_order_fields(order: OrderRecord) -> bool:
    """Return True if Note, Internal Note, Note Print, and Products are all empty."""
    note = (order.note or "").strip()
    inote = (order.internal_note or "").strip()
    nprint = (order.note_print or "").strip()
    prod = (order.products or "").strip()
    return (
        note in {"", "-"}
        and inote in {"", "-"}
        and nprint in {"", "-"}
        and prod in {"", "-", "Empty Cart"}
    )


def products_overlap(order_a: OrderRecord, order_b: OrderRecord) -> Tuple[str, str]:
    """Compare two orders' products -> ('MATCH' | 'DIFFERENT' | 'AMBIGUOUS', detail)."""
    names_a, skus_a = extract_product_keys(order_a.internal_note, order_a.products)
    names_b, skus_b = extract_product_keys(order_b.internal_note, order_b.products)

    if not (names_a or skus_a) or not (names_b or skus_b):
        return "AMBIGUOUS", "Thiếu thông tin sản phẩm để đối chiếu"

    sku_overlap = skus_a & skus_b
    if sku_overlap:
        return "MATCH", f"Trùng SKU {', '.join(sorted(sku_overlap))}"

    name_overlap = names_a & names_b
    if name_overlap:
        return "MATCH", f"Trùng SP {', '.join(sorted(name_overlap))}"

    # Fallback partial containment when one side has a generic base like "mop" vs "blue floor mop"
    for a in names_a:
        for b in names_b:
            if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
                return "MATCH", f"Trùng SP '{a}' / '{b}'"

    return "DIFFERENT", f"Khác SP ({sorted(names_a or skus_a)} vs {sorted(names_b or skus_b)})"


def classify_step2_orders(orders: Sequence[OrderRecord]) -> Tuple[List[ClassificationResult], List[OrderRecord]]:
    """Step 2: Identify New FILL_IN orders in the 3 GIPs with Return Rate >= 50%."""
    to_convert: List[ClassificationResult] = []
    already_call: List[OrderRecord] = []
    for o in orders:
        if o.return_rate is None or o.return_rate < STEP2_RETURN_RATE_THRESHOLD:
            continue
        svc = o.service_types.upper()
        if "FILL_IN" in svc:
            to_convert.append(
                ClassificationResult(
                    order=o,
                    bucket=TAG_CALL_TO_CF,
                    action="CONVERT_TO_CALL",
                    tag=TAG_CALL_TO_CF,
                    reason=f"Return Rate {o.return_rate:.0f}% (>= 50%) tại {o.shop}",
                )
            )
        elif "CALL" in svc:
            already_call.append(o)
    return to_convert, already_call


def classify_step3a_orders(
    orders: Sequence[OrderRecord],
    histories: Dict[str, PhoneHistory],
    ref_date: datetime,
) -> List[ClassificationResult]:
    """Classify all Step 3A (Duplicated Phone, FILL_IN) orders in strict SOP priority."""
    results: List[ClassificationResult] = []

    # Sort newest first so we know which New orders are canceled as DUPLICATE when evaluating older peers
    indexed_with_dt: List[Tuple[OrderRecord, datetime]] = []
    for o in orders:
        dt = parse_created_at(o.created_at, ref_date) or ref_date
        indexed_with_dt.append((o, dt))

    # Build normalized history lookup
    norm_histories: Dict[str, PhoneHistory] = dict(histories)

    # First pass: evaluate in newest-to-oldest order while tracking IDs marked DUPLICATE
    ordered_desc = sorted(indexed_with_dt, key=lambda x: (x[1], x[0].id), reverse=True)
    decision_by_id: Dict[str, ClassificationResult] = {}
    canceled_new_ids: Set[str] = set()

    for order, order_dt in ordered_desc:
        hist = norm_histories.get(order.normalized_phone)
        if hist is None:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason="Không tìm thấy lịch sử SĐT",
            )
            continue

        # Merge any peer Step 3A orders not yet reflected in history (should already be in history)
        seen_ids = {h.id for h in hist.orders}
        merged_orders = list(hist.orders)
        for peer, _ in indexed_with_dt:
            if peer.normalized_phone == order.normalized_phone and peer.id not in seen_ids:
                merged_orders.append(peer)
                seen_ids.add(peer.id)

        other_orders = [h for h in merged_orders if h.id != order.id]
        returned_total = max(
            hist.returned_count,
            sum(1 for h in other_orders if h.status == "Returned"),
        )

        # Priority 1: TEST (order fields OR any history order tagged TEST / test hit)
        own_test, own_snippet = has_test_order_flag(order)
        hist_test_ids = []
        hist_test_ambiguous = []
        for h in other_orders:
            h_flag, h_snip = has_test_order_flag(h)
            if h_flag == "DEFINITE":
                hist_test_ids.append(f"{h.id} ({h_snip})")
            elif h_flag == "AMBIGUOUS":
                hist_test_ambiguous.append(f"{h.id} ({h_snip})")

        if own_test == "DEFINITE" or hist_test_ids or hist.has_test_hit:
            refs = [x.split()[0] for x in hist_test_ids]
            why = own_snippet if own_test == "DEFINITE" else f"Lịch sử SĐT có đơn TEST: {', '.join(hist_test_ids or ['scan'])}"
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_TEST,
                action="CANCEL",
                tag=TAG_TEST,
                cancel_reason_pattern=r"^Test",
                reason=why,
                reference_ids=refs,
                returned_count=returned_total,
            )
            continue

        is_deferred_gip = order.gip_key in DEFERRED_FILL_GIPS
        is_empty_order = is_empty_order_fields(order)
        is_fresh_empty = (not is_deferred_gip) and is_empty_order and is_order_younger_than(
            order, ref_date, EMPTY_ORDER_COOLDOWN_MINUTES
        )

        # Confirmed completely empty order -> user rule: DUPLICATE & cancel (except deferred-fill GIPs & fresh orders)
        if (not is_deferred_gip) and (not is_fresh_empty) and order.empty_detail_confirmed is True:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DUPLICATE,
                action="CANCEL",
                tag=TAG_DUPLICATE,
                cancel_reason_pattern=r"^Duplicate",
                reason="Đơn trống cả Note, Internal Note, Note Print và giỏ hàng (Empty Cart)",
                returned_count=returned_total,
            )
            canceled_new_ids.add(order.id)
            continue

        # Priority 2: DUPLICATE (same GIP, <= 30 days, active status, matching product)
        dup_matches: List[Tuple[OrderRecord, str]] = []
        ambiguous_active_same_gip: List[OrderRecord] = []
        active_diff_30d: List[OrderRecord] = []

        for h in other_orders:
            if h.id in canceled_new_ids:
                continue
            if h.status not in ACTIVE_STATUSES:
                continue
            h_dt = parse_created_at(h.created_at, ref_date)
            if not is_within_days(h_dt, ref_date, DUPLICATE_WINDOW_DAYS):
                continue

            if h.status == "New":
                # Between two New orders, only cancel the newer one
                h_cmp = (h_dt or ref_date, h.id)
                o_cmp = (order_dt, order.id)
                if o_cmp <= h_cmp:
                    # order is older than h; check overlap for DIFFERENT PRODUCT if different product
                    ov, _ = products_overlap(order, h)
                    if ov == "DIFFERENT":
                        active_diff_30d.append(h)
                    elif ov == "AMBIGUOUS" and order.gip_key == h.gip_key:
                        if is_deferred_gip:
                            active_diff_30d.append(h)
                        else:
                            ambiguous_active_same_gip.append(h)
                    continue

            if order.gip_key == h.gip_key:
                ov, detail = products_overlap(order, h)
                if ov == "MATCH":
                    dup_matches.append((h, detail))
                elif ov == "AMBIGUOUS":
                    if is_deferred_gip:
                        active_diff_30d.append(h)
                    else:
                        ambiguous_active_same_gip.append(h)
                else:
                    active_diff_30d.append(h)
            else:
                active_diff_30d.append(h)

        if dup_matches:
            ref_list = [f"{m[0].id} ({m[0].status})" for m in dup_matches]
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DUPLICATE,
                action="CANCEL",
                tag=TAG_DUPLICATE,
                cancel_reason_pattern=r"^Duplicate",
                reason=f"Trùng SP cùng {order.gip_key} trong 30 ngày với {', '.join(ref_list)}",
                reference_ids=[m[0].id for m in dup_matches],
                returned_count=returned_total,
            )
            canceled_new_ids.add(order.id)
            continue

        # If product is missing/ambiguous and another active order exists in the SAME GIP within 30 days
        if ambiguous_active_same_gip:
            refs = [h.id for h in ambiguous_active_same_gip]
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason=(
                    f"Thiếu/không rõ tên SP để đối chiếu cùng {order.gip_key} với đơn đang xử lý {', '.join(refs)}"
                ),
                reference_ids=refs,
                returned_count=returned_total,
            )
            continue

        # Priority 3: MULTIPLE RETURNED (>= 3 Returned across entire history)
        if returned_total >= MULTIPLE_RETURNED_THRESHOLD:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_MULTIPLE_RETURNED,
                action="CANCEL",
                tag=TAG_MULTIPLE_RETURNED,
                cancel_reason_pattern=r"^Multiple returned",
                reason=f"Lịch sử SĐT có {returned_total} đơn Returned (>= {MULTIPLE_RETURNED_THRESHOLD})",
                returned_count=returned_total,
            )
            continue

        # Ambiguous weak 'test' text check before allowing assignment
        if own_test == "AMBIGUOUS" or hist_test_ambiguous:
            detail = own_snippet or ", ".join(hist_test_ambiguous)
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason=f"Nghi ngờ từ khóa test: {detail}",
                returned_count=returned_total,
            )
            continue

        # Deferred-fill GIPs (GIP 244 / GIP 300): empty orders get products filled later -> DIFFERENT PRODUCT & assign
        if is_deferred_gip and is_empty_order:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DIFFERENT_PRODUCT,
                action="ASSIGN",
                tag=TAG_DIFFERENT_PRODUCT,
                reason=f"Đơn trống thuộc {order.gip_key} (shop sẽ fill in dữ liệu sau) -> gán DIFFERENT PRODUCT & chia sale",
                reference_ids=[h.id for h in active_diff_30d[:3]],
                returned_count=returned_total,
            )
            continue

        # Fresh empty orders on non-deferred GIPs (< cooldown) wait for next cycle before canceling
        if is_fresh_empty:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason=f"Đơn trống mới tạo (< {EMPTY_ORDER_COOLDOWN_MINUTES} phút), tạm chờ đồng bộ dữ liệu rồi kiểm tra lại ở lượt sau",
                returned_count=returned_total,
            )
            continue

        # Priority 4: DIFFERENT PRODUCT
        # (a) Active non-final order within 30 days with different product (takes priority over Delivered/Returning/Returned < 3)
        if active_diff_30d:
            refs = [f"{h.id} ({h.status})" for h in active_diff_30d[:3]]
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DIFFERENT_PRODUCT,
                action="ASSIGN",
                tag=TAG_DIFFERENT_PRODUCT,
                reason=f"Có đơn đang xử lý khác SP: {', '.join(refs)}; Returned={returned_total}",
                reference_ids=[h.id for h in active_diff_30d],
                returned_count=returned_total,
            )
            continue

        # (b) Only Canceled orders in history
        if other_orders and all(h.status in {"Canceled", "Cancelled"} for h in other_orders) and not hist.incomplete_scan:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DIFFERENT_PRODUCT,
                action="ASSIGN",
                tag=TAG_DIFFERENT_PRODUCT,
                reason=f"Lịch sử chỉ có {len(other_orders)} đơn Canceled",
                reference_ids=[h.id for h in other_orders[:3]],
                returned_count=returned_total,
            )
            continue

        # Priority 5: RE ORDER (Delivered / Returning / Returned < 3)
        has_terminal_reorder = (
            hist.tab_counts.get("Delivered", 0) > 0
            or hist.tab_counts.get("Returning", 0) > 0
            or returned_total > 0
            or any(h.status in {"Delivered", "Returning", "Returned"} for h in other_orders)
        )
        if has_terminal_reorder:
            refs = [
                f"{h.id} ({h.status})"
                for h in other_orders
                if h.status in {"Delivered", "Returning", "Returned"}
            ][:3]
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_RE_ORDER,
                action="ASSIGN",
                tag=TAG_RE_ORDER,
                reason=f"Không có đơn trùng đang chạy; lịch sử đã nhận/hoàn (<3): {', '.join(refs) or f'Returned={returned_total}'}",
                reference_ids=[h.id for h in other_orders[:3]],
                returned_count=returned_total,
            )
            continue

        # Fallback: e.g. only >30d Pending or <2 total orders
        decision_by_id[order.id] = ClassificationResult(
            order=order,
            bucket="AMBIGUOUS",
            action="REVIEW",
            reason=f"Lịch sử SĐT bất thường (All={hist.all_count}, Returned={returned_total}), cần kiểm tra",
            returned_count=returned_total,
        )

    # Preserve original table order
    for o in orders:
        results.append(decision_by_id[o.id])
    return results


def classify_step3b_orders(
    orders: Sequence[OrderRecord],
    histories: Dict[str, PhoneHistory],
    ref_date: Optional[datetime] = None,
) -> List[ClassificationResult]:
    """Classify Step 3B (Not Duplicated Phone, FILL_IN) orders for TEST and cross-history checks."""
    results: List[ClassificationResult] = []
    for o in orders:
        test_flag, snippet = has_test_order_flag(o)
        if test_flag == "DEFINITE":
            results.append(
                ClassificationResult(
                    order=o,
                    bucket=TAG_TEST,
                    action="CANCEL",
                    tag=TAG_TEST,
                    cancel_reason_pattern=r"^Test",
                    reason=f"Phát hiện từ khóa TEST trên đơn: {snippet}",
                )
            )
            continue

        if test_flag == "AMBIGUOUS":
            results.append(
                ClassificationResult(
                    order=o,
                    bucket="AMBIGUOUS",
                    action="REVIEW",
                    reason=f"Nghi ngờ từ khóa test: {snippet}",
                )
            )
            continue

        # Cross-check if this phone also appeared in Step 3A histories
        hist = histories.get(o.normalized_phone)
        if hist is not None:
            hist_test = any(has_test_order_flag(h)[0] == "DEFINITE" for h in hist.orders) or hist.has_test_hit
            if hist_test:
                results.append(
                    ClassificationResult(
                        order=o,
                        bucket=TAG_TEST,
                        action="CANCEL",
                        tag=TAG_TEST,
                        cancel_reason_pattern=r"^Test",
                        reason="Giao thoa với lịch sử 3A có đơn gắn tag TEST",
                        returned_count=hist.returned_count,
                    )
                )
                continue

        if hist is not None and hist.returned_count >= MULTIPLE_RETURNED_THRESHOLD:
            results.append(
                ClassificationResult(
                    order=o,
                    bucket=TAG_MULTIPLE_RETURNED,
                    action="CANCEL",
                    tag=TAG_MULTIPLE_RETURNED,
                    cancel_reason_pattern=r"^Multiple returned",
                    reason=f"Lọt từ Not Duplicated Phone nhưng lịch sử SĐT có {hist.returned_count} đơn Returned (>= {MULTIPLE_RETURNED_THRESHOLD})",
                    returned_count=hist.returned_count,
                )
            )
            continue

        is_deferred_gip = o.gip_key in DEFERRED_FILL_GIPS
        is_empty_order = is_empty_order_fields(o)
        if is_deferred_gip and is_empty_order:
            results.append(
                ClassificationResult(
                    order=o,
                    bucket="ASSIGN_ONLY",
                    action="ASSIGN",
                    tag="",
                    reason=f"Not Duplicated Phone hợp lệ (đơn trống thuộc {o.gip_key} sẽ fill in dữ liệu sau)",
                )
            )
            continue

        if not is_deferred_gip:
            if is_empty_order and is_order_younger_than(o, ref_date, EMPTY_ORDER_COOLDOWN_MINUTES):
                results.append(
                    ClassificationResult(
                        order=o,
                        bucket="AMBIGUOUS",
                        action="REVIEW",
                        reason=f"Đơn trống mới tạo (< {EMPTY_ORDER_COOLDOWN_MINUTES} phút), tạm chờ đồng bộ dữ liệu",
                        returned_count=hist.returned_count if hist is not None else 0,
                    )
                )
                continue
            if o.empty_detail_confirmed is True:
                results.append(
                    ClassificationResult(
                        order=o,
                        bucket=TAG_DUPLICATE,
                        action="CANCEL",
                        tag=TAG_DUPLICATE,
                        cancel_reason_pattern=r"^Duplicate",
                        reason="Đơn trống cả Note, Internal Note, Note Print và giỏ hàng (Empty Cart)",
                        returned_count=hist.returned_count if hist is not None else 0,
                    )
                )
                continue

        results.append(
            ClassificationResult(
                order=o,
                bucket="ASSIGN_ONLY",
                action="ASSIGN",
                tag="",
                reason="Not Duplicated Phone hợp lệ (không dính TEST)",
            )
        )
    return results


# ----------------------------------------------------------------------
# GIP 287 (WeUpBook) dedicated check rules (independent from FILL IN DATA)
# ----------------------------------------------------------------------
_GIP287_TOOLTIP_RE = re.compile(
    r"Success\s*:\s*(\d+)\s*[-–|·•]\s*Returned\s*:\s*(\d+)",
    re.IGNORECASE,
)
_GIP287_COMBO_RE = re.compile(r"\b\d+\.CB(\d+)\.((?:[A-Z]\d+\.)*[A-Z]\d+)\b", re.IGNORECASE)
_GIP287_SINGLE_RE = re.compile(r"\b\d+\.((?:[A-Z]\d+\.)*[A-Z]\d+)\b", re.IGNORECASE)


def parse_return_rate_tooltip(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse 'Success: X - Returned: Y' from the Return Rate cell tooltip."""
    m = _GIP287_TOOLTIP_RE.search(text or "")
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _extract_gip287_from_text(text: str, from_detail: bool) -> Tuple[Set[str], Optional[int], bool]:
    s = (text or "").strip()
    if not s:
        return set(), None, False

    m_combo = _GIP287_COMBO_RE.search(s)
    if m_combo:
        expected = int(m_combo.group(1))
        items = {tok.upper() for tok in m_combo.group(2).split(".") if tok}
        tail = s[m_combo.end() :]
        tail_stripped = tail.strip()
        is_truncated = len(items) < expected or (
            not from_detail
            and (
                tail.startswith("...")
                or tail.startswith(".")
                or (tail_stripped != "" and not re.match(r"^(?:\||\(|\s)", tail))
            )
        )
        return items, expected, is_truncated

    m_single = _GIP287_SINGLE_RE.search(s)
    if m_single:
        items = {tok.upper() for tok in m_single.group(1).split(".") if tok}
        return items, None, False

    return set(), None, False


def extract_gip287_items(
    internal_note: str,
    detail_sku_code: str = "",
) -> Tuple[Set[str], Optional[int], bool]:
    """Extract normalized GIP 287 book/ebook tokens ({'G354', 'E294', ...}, expected_cnt, is_truncated)."""
    if (detail_sku_code or "").strip():
        items, exp, trunc = _extract_gip287_from_text(detail_sku_code, from_detail=True)
        if items:
            return items, exp, trunc
    return _extract_gip287_from_text(internal_note, from_detail=False)


def _gip287_has_need_pay_tag(order: OrderRecord) -> bool:
    return any(
        t.strip().upper() in {"NEED PAY ADVANCED", "NEED PAY IN ADVANCE"}
        for t in order.tags
    )


def classify_gip287_phase1_orders(
    orders: Sequence[OrderRecord],
) -> Tuple[List[ClassificationResult], List[OrderRecord]]:
    """Phase 1 for GIP 287: Return Rate >= 60% AND Returned > 3 (>= 4) -> NEED PAY ADVANCED (keep Service Type)."""
    results: List[ClassificationResult] = []
    already_tagged: List[OrderRecord] = []

    for o in orders:
        if _gip287_has_need_pay_tag(o):
            already_tagged.append(o)
            continue
        if o.return_rate is None or o.return_rate < GIP287_RETURN_RATE_THRESHOLD:
            continue
        if o.return_rate_returned is None:
            results.append(
                ClassificationResult(
                    order=o,
                    bucket="AMBIGUOUS",
                    action="REVIEW",
                    reason=(
                        f"Return Rate {o.return_rate:.0f}% (>= {GIP287_RETURN_RATE_THRESHOLD:.0f}%) "
                        "nhưng không đọc được tooltip số đơn hoàn"
                    ),
                )
            )
            continue
        if o.return_rate_returned >= GIP287_MIN_RETURNED_COUNT:
            results.append(
                ClassificationResult(
                    order=o,
                    bucket=TAG_NEED_PAY_ADVANCED,
                    action="TAG_ONLY",
                    tag=TAG_NEED_PAY_ADVANCED,
                    reason=(
                        f"Return Rate {o.return_rate:.0f}% (>= 60%) và "
                        f"Returned={o.return_rate_returned} (> 3)"
                    ),
                    returned_count=o.return_rate_returned,
                )
            )

    return results, already_tagged


def classify_gip287_phase1_test_orders(
    orders: Sequence[OrderRecord],
) -> List[ClassificationResult]:
    """Scan all New GIP 287 orders (including Not Duplicated Phone) for TEST keywords/tag."""
    results: List[ClassificationResult] = []
    for o in orders:
        flag, snippet = has_test_order_flag(o)
        if flag == "DEFINITE":
            results.append(
                ClassificationResult(
                    order=o,
                    bucket=TAG_TEST,
                    action="CANCEL",
                    tag=TAG_TEST,
                    cancel_reason_pattern=r"^Test",
                    reason=f"Phát hiện từ khóa TEST trên đơn New GIP 287 ({snippet})",
                )
            )
        elif flag == "AMBIGUOUS":
            results.append(
                ClassificationResult(
                    order=o,
                    bucket="AMBIGUOUS",
                    action="REVIEW",
                    reason=f"Nghi ngờ từ khóa test trên đơn New GIP 287: {snippet}",
                )
            )
    return results


def _item_book_number(token: str) -> str:
    return token[1:] if len(token) >= 2 and token[0].upper() in {"G", "E"} else token


def _cross_format_same_titles(items_a: Set[str], items_b: Set[str]) -> List[str]:
    nums_a = {_item_book_number(x) for x in items_a if x.upper().startswith("G")}
    nums_b_e = {_item_book_number(x) for x in items_b if x.upper().startswith("E")}
    nums_a_e = {_item_book_number(x) for x in items_a if x.upper().startswith("E")}
    nums_b_g = {_item_book_number(x) for x in items_b if x.upper().startswith("G")}
    return sorted((nums_a & nums_b_e) | (nums_a_e & nums_b_g))


def classify_gip287_phase2_orders(
    orders: Sequence[OrderRecord],
    histories: Dict[str, PhoneHistory],
    ref_date: datetime,
) -> Tuple[List[ClassificationResult], List[ClassificationResult]]:
    """Classify GIP 287 Duplicated Phone New orders -> (new_order_results, peer_cancel_results)."""
    indexed_with_dt: List[Tuple[OrderRecord, datetime]] = []
    for o in orders:
        dt = parse_created_at(o.created_at, ref_date) or ref_date
        indexed_with_dt.append((o, dt))

    ordered_desc = sorted(indexed_with_dt, key=lambda x: (x[1], x[0].id), reverse=True)
    decision_by_id: Dict[str, ClassificationResult] = {}
    canceled_ids: Set[str] = set()
    peer_cancel_by_id: Dict[str, ClassificationResult] = {}

    for order, order_dt in ordered_desc:
        hist = histories.get(order.normalized_phone)
        if hist is None or hist.incomplete_scan:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason="Không đọc được đầy đủ lịch sử SĐT để đối chiếu GIP 287",
            )
            continue

        seen_ids = {h.id for h in hist.orders}
        merged_orders = list(hist.orders)
        for peer, _ in indexed_with_dt:
            if peer.normalized_phone == order.normalized_phone and peer.id not in seen_ids:
                merged_orders.append(peer)
                seen_ids.add(peer.id)

        other_orders = [h for h in merged_orders if h.id != order.id]

        # Priority 1: TEST (always filter TEST first across phone history)
        own_test, own_snippet = has_test_order_flag(order)
        hist_test_ids: List[str] = []
        hist_test_ambiguous: List[str] = []
        for h in other_orders:
            h_flag, h_snip = has_test_order_flag(h)
            if h_flag == "DEFINITE":
                hist_test_ids.append(f"{h.id} ({h_snip})")
            elif h_flag == "AMBIGUOUS":
                hist_test_ambiguous.append(f"{h.id} ({h_snip})")

        if own_test == "DEFINITE" or hist_test_ids or hist.has_test_hit:
            refs = [x.split()[0] for x in hist_test_ids]
            why = own_snippet if own_test == "DEFINITE" else f"Lịch sử SĐT có đơn TEST: {', '.join(hist_test_ids or ['scan'])}"
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_TEST,
                action="CANCEL",
                tag=TAG_TEST,
                cancel_reason_pattern=r"^Test",
                reason=why,
                reference_ids=refs,
            )
            canceled_ids.add(order.id)
            continue

        if own_test == "AMBIGUOUS" or hist_test_ambiguous:
            detail = own_snippet or ", ".join(hist_test_ambiguous)
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason=f"Nghi ngờ từ khóa test: {detail}",
            )
            continue

        # Filter history strictly to non-Canceled GIP 287 orders (and exclude already canceled peers)
        gip287_peers = [
            h
            for h in other_orders
            if h.gip_key == GIP287_GIP_KEY
            and h.status not in {"Canceled", "Cancelled"}
            and h.id not in canceled_ids
        ]

        recent_peers: List[Tuple[OrderRecord, datetime]] = []
        older_peers: List[OrderRecord] = []
        missing_dt_peers: List[str] = []

        for h in gip287_peers:
            h_dt = parse_created_at(h.created_at, ref_date)
            if h_dt is None:
                missing_dt_peers.append(h.id)
                continue
            if is_within_days(h_dt, ref_date, GIP287_DUPLICATE_WINDOW_DAYS):
                recent_peers.append((h, h_dt))
            else:
                older_peers.append(h)

        if missing_dt_peers:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason=f"Không đọc được ngày tạo Created At của đơn GIP 287: {', '.join(missing_dt_peers)}",
                reference_ids=missing_dt_peers,
            )
            continue

        if not recent_peers:
            older_final = [h for h in older_peers if h.status in GIP287_FINAL_STATUSES]
            if older_final:
                refs = [h.id for h in older_final]
                decision_by_id[order.id] = ClassificationResult(
                    order=order,
                    bucket=TAG_DIFFERENT_PRODUCT,
                    action="TAG_ONLY",
                    tag=TAG_DIFFERENT_PRODUCT,
                    reason=(
                        f"Chỉ có đơn GIP 287 cũ hơn {GIP287_DUPLICATE_WINDOW_DAYS} ngày đã đạt trạng thái cuối "
                        f"({', '.join(f'{h.id}:{h.status}' for h in older_final[:3])}) -> coi như đơn mới (DIFFERENT PRODUCT)"
                    ),
                    reference_ids=refs,
                )
            else:
                decision_by_id[order.id] = ClassificationResult(
                    order=order,
                    bucket="NO_ACTION",
                    action="NO_ACTION",
                    reason="Không có đơn GIP 287 hiệu lực đối chiếu (lịch sử chỉ có đơn Canceled hoặc GIP khác)",
                    reference_ids=[h.id for h in other_orders[:3]],
                )
            continue

        items_o, _, trunc_o = extract_gip287_items(order.internal_note, order.detail_sku_code)
        if not items_o or trunc_o:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason=f"Thiếu hoặc bị cắt ngắn mã sản phẩm GIP 287 trên đơn {order.id}",
                reference_ids=[h.id for h, _ in recent_peers],
            )
            continue

        cancel_self_refs: List[Tuple[OrderRecord, str]] = []
        cancel_peer_small: List[Tuple[OrderRecord, str]] = []
        review_reasons: List[str] = []
        multiple_orders_refs: List[Tuple[OrderRecord, str]] = []
        diff_product_refs: List[Tuple[OrderRecord, str]] = []

        for h, h_dt in recent_peers:
            items_h, _, trunc_h = extract_gip287_items(h.internal_note, h.detail_sku_code)
            if not items_h or trunc_h:
                review_reasons.append(f"Thiếu/bị cắt mã SP trên đơn {h.id} ({h.status})")
                continue

            h_final = h.status in GIP287_FINAL_STATUSES
            inter = items_o & items_h

            # TH2: 100% identical items
            if items_o == items_h:
                if h_final or h.status != "New" or (h_dt, h.id) < (order_dt, order.id):
                    cancel_self_refs.append((h, f"trùng 100% với {h.id} ({h.status})"))
                continue

            # TH3a: current order is a strict subset (smaller) of h -> cancel smaller order (self)
            if items_o < items_h:
                cancel_self_refs.append(
                    (
                        h,
                        f"đơn nhỏ ({len(items_o)}/{len(items_h)} SP) nằm gọn trong {h.id} ({h.status})",
                    )
                )
                continue

            # TH3b: h is a strict subset (smaller) of current order
            if items_h < items_o:
                if h_final:
                    review_reasons.append(
                        f"TH3 đơn nhỏ {h.id} ({h.status}) đã gửi hàng, đơn mới {order.id} là combo lớn hơn (cần sửa/gỡ SP tay)"
                    )
                elif h.status in GIP287_CANCELABLE_SMALL_STATUSES:
                    cancel_peer_small.append(
                        (
                            h,
                            f"đơn nhỏ {h.id} ({h.status}, {len(items_h)}/{len(items_o)} SP) nằm gọn trong đơn lớn {order.id}",
                        )
                    )
                else:
                    review_reasons.append(
                        f"TH3 đơn nhỏ {h.id} đang ở trạng thái {h.status} (đã qua Pending) nên không tự hủy"
                    )
                continue

            # Partial overlap (not equal and neither is subset)
            if inter:
                ratio = len(inter) / len(items_o)
                pct = int(round(ratio * 100))
                shared_txt = ", ".join(sorted(inter))
                if h_final:
                    if ratio >= 0.5:
                        review_reasons.append(
                            f"TH5 trùng {len(inter)}/{len(items_o)} SP ({pct}% >= 50%: {shared_txt}) với đơn đã gửi {h.id} ({h.status}) -> cần gỡ SP trùng hoặc duyệt tay"
                        )
                    else:
                        diff_product_refs.append(
                            (
                                h,
                                f"trùng {len(inter)}/{len(items_o)} SP ({pct}% < 50%) với đơn đã đạt trạng thái cuối {h.id} ({h.status})",
                            )
                        )
                else:
                    multiple_orders_refs.append(
                        (
                            h,
                            f"hai đơn đang xử lý dùng chung {len(inter)}/{len(items_o)} SP ({pct}%: {shared_txt}) với {h.id} ({h.status})",
                        )
                    )
                continue

            # TH1: 0 exact overlap (completely different products OR same title across Book vs Ebook)
            cross = _cross_format_same_titles(items_o, items_h)
            if cross:
                diff_product_refs.append(
                    (h, f"cùng tên sách nhưng khác loại Book/Ebook (#{', #'.join(cross)}) với {h.id} ({h.status})")
                )
            else:
                diff_product_refs.append((h, f"không trùng SP (0%) với {h.id} ({h.status})"))

        # Decision priority for order
        if cancel_self_refs:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DUPLICATE,
                action="CANCEL",
                tag=TAG_DUPLICATE,
                cancel_reason_pattern=r"^Duplicate",
                reason="; ".join(detail for _, detail in cancel_self_refs),
                reference_ids=[h.id for h, _ in cancel_self_refs],
            )
            canceled_ids.add(order.id)
            continue

        if review_reasons:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="AMBIGUOUS",
                action="REVIEW",
                reason="; ".join(review_reasons),
                reference_ids=[h.id for h, _ in recent_peers],
            )
            continue

        for h, detail in cancel_peer_small:
            canceled_ids.add(h.id)
            if h.status != "New" and h.id not in peer_cancel_by_id:
                peer_cancel_by_id[h.id] = ClassificationResult(
                    order=h,
                    bucket=TAG_DUPLICATE,
                    action="CANCEL",
                    tag=TAG_DUPLICATE,
                    cancel_reason_pattern=r"^Duplicate",
                    reason=f"Đơn nhỏ {h.id} ({h.status}) nằm trong đơn lớn {order.id} (New) -> hủy đơn nhỏ & gắn DUPLICATE ({detail})",
                    reference_ids=[order.id],
                )

        if multiple_orders_refs:
            all_refs = [h.id for h, _ in multiple_orders_refs] + [h.id for h, _ in diff_product_refs]
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_MULTIPLE_ORDERS,
                action="TAG_ONLY",
                tag=TAG_MULTIPLE_ORDERS,
                reason="; ".join(detail for _, detail in multiple_orders_refs),
                reference_ids=all_refs,
            )
        elif diff_product_refs:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DIFFERENT_PRODUCT,
                action="TAG_ONLY",
                tag=TAG_DIFFERENT_PRODUCT,
                reason="; ".join(detail for _, detail in diff_product_refs),
                reference_ids=[h.id for h, _ in diff_product_refs],
            )
        elif any(h.status in GIP287_FINAL_STATUSES for h in older_peers):
            older_final = [h for h in older_peers if h.status in GIP287_FINAL_STATUSES]
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket=TAG_DIFFERENT_PRODUCT,
                action="TAG_ONLY",
                tag=TAG_DIFFERENT_PRODUCT,
                reason=(
                    f"Đơn nhỏ gần nhất đã bị hủy trùng và khách có đơn GIP 287 cũ đạt trạng thái cuối "
                    f"({', '.join(f'{h.id}:{h.status}' for h in older_final[:3])})"
                ),
                reference_ids=[h.id for h in older_final],
            )
        else:
            decision_by_id[order.id] = ClassificationResult(
                order=order,
                bucket="NO_ACTION",
                action="NO_ACTION",
                reason="Đơn nhỏ trùng nhau đã hủy bớt; giữ nguyên đơn hiện tại",
                reference_ids=[h.id for h, _ in cancel_peer_small],
            )

    new_results = [decision_by_id[o.id] for o in orders]
    return new_results, list(peer_cancel_by_id.values())
