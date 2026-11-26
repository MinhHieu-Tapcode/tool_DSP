"""Step 1: Read daily attendance for Fill-In telesale staff from Google Sheets or CLI override."""

from __future__ import annotations

import csv
import io
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from config import ATTENDANCE_SPREADSHEET_ID, FILLIN_STAFF

TZ_VN = timezone(timedelta(hours=7))


@dataclass
class StaffStatus:
    """Attendance status for a single Fill-In telesale staff member."""

    code: str
    name: str
    account: str
    raw_value: str
    is_working: bool


@dataclass
class AttendanceResult:
    """Result of resolving who is working on a given date."""

    target_date: date
    date_header: str
    source: str
    tab_name: str
    staff_statuses: List[StaffStatus] = field(default_factory=list)
    active_accounts: List[str] = field(default_factory=list)


def today_vn() -> date:
    """Return today's date in Asia/Ho_Chi_Minh (UTC+7)."""
    return datetime.now(TZ_VN).date()


def parse_target_date(date_str: Optional[str]) -> date:
    """Parse DD/MM/YYYY (or YYYY-MM-DD) or default to today in UTC+7."""
    if not date_str:
        return today_vn()
    s = date_str.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Định dạng ngày không hợp lệ: '{date_str}'. Dùng DD/MM/YYYY (vd: 24/11/2026).")


def resolve_sales_override(sales_arg: str, target_date: date) -> AttendanceResult:
    """Resolve --sales comma-separated override into G-solution account names."""
    raw_items = [x.strip() for x in sales_arg.split(",") if x.strip()]
    if not raw_items:
        raise ValueError("Tham số --sales không được để trống.")

    active_accounts: List[str] = []
    statuses: List[StaffStatus] = []
    matched_accounts = set()

    for item in raw_items:
        low = item.lower()
        found_staff: Optional[Dict[str, str]] = None
        for st in FILLIN_STAFF:
            aliases = [a.strip().lower() for a in st["aliases"].split(",")]
            if low == st["account"].lower() or low in aliases:
                found_staff = st
                break
        acct = found_staff["account"] if found_staff else item
        if acct not in active_accounts:
            active_accounts.append(acct)
        if found_staff:
            matched_accounts.add(acct)
            statuses.append(
                StaffStatus(
                    code=found_staff["code"],
                    name=found_staff["name"],
                    account=acct,
                    raw_value="OVERRIDE",
                    is_working=True,
                )
            )
        else:
            statuses.append(
                StaffStatus(
                    code="-",
                    name=item,
                    account=acct,
                    raw_value="OVERRIDE",
                    is_working=True,
                )
            )

    for st in FILLIN_STAFF:
        if st["account"] not in matched_accounts:
            statuses.append(
                StaffStatus(
                    code=st["code"],
                    name=st["name"],
                    account=st["account"],
                    raw_value="SKIPPED_BY_OVERRIDE",
                    is_working=False,
                )
            )

    return AttendanceResult(
        target_date=target_date,
        date_header=target_date.strftime("%d/%m"),
        source="cli_override",
        tab_name="-",
        staff_statuses=statuses,
        active_accounts=active_accounts,
    )


def _tab_candidates(target_date: date) -> List[str]:
    """Generate likely sheet tab names for the target month/year."""
    month_upper = target_date.strftime("%B").upper()
    month_title = target_date.strftime("%B")
    year_str = str(target_date.year)
    candidates = [
        f"{month_upper} {year_str}",
        f"{month_upper} {year_str} ",
        f"{month_title} {year_str}",
        f"{month_title} {year_str} ",
        month_upper,
        f"{month_upper} ",
        month_title,
    ]
    seen = set()
    unique: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _extract_month_year(rows: Sequence[Sequence[str]]) -> tuple[Optional[int], Optional[int]]:
    """Extract Month and Year markers from the top metadata rows of an attendance sheet."""
    found_month: Optional[int] = None
    found_year: Optional[int] = None
    for row in rows[:6]:
        for idx, cell in enumerate(row):
            label = cell.strip().lower()
            if label == "month" and idx + 1 < len(row):
                val = row[idx + 1].strip()
                if val.isdigit():
                    found_month = int(val)
            elif label == "year" and idx + 1 < len(row):
                val = row[idx + 1].strip()
                if val.isdigit():
                    found_year = int(val)
    return found_month, found_year


def parse_attendance_rows(
    rows: Sequence[Sequence[str]],
    target_date: date,
    tab_name: str,
    source: str,
) -> Optional[AttendanceResult]:
    """Validate sheet metadata/headers and extract Fill-In staff attendance for target_date."""
    if not rows:
        return None

    sheet_month, sheet_year = _extract_month_year(rows)
    if sheet_month is not None and sheet_month != target_date.month:
        return None
    if sheet_year is not None and sheet_year != target_date.year:
        return None

    date_header = target_date.strftime("%d/%m")
    alt_date_header = f"{target_date.day}/{target_date.month}"

    date_col: Optional[int] = None
    for row in rows[:10]:
        for idx, cell in enumerate(row):
            c = cell.strip()
            if c == date_header or c == alt_date_header:
                date_col = idx
                break
        if date_col is not None:
            break

    if date_col is None:
        return None

    code_col = 1
    name_col = 2
    for row in rows[:10]:
        upper_cells = [c.strip().upper() for c in row]
        if "CODE" in upper_cells and "NAME" in upper_cells:
            code_col = upper_cells.index("CODE")
            name_col = upper_cells.index("NAME")
            break

    statuses: List[StaffStatus] = []
    active_accounts: List[str] = []

    for staff in FILLIN_STAFF:
        matched_row: Optional[Sequence[str]] = None
        for row in rows:
            code_val = row[code_col].strip().upper() if code_col < len(row) else ""
            name_val = row[name_col].strip().lower() if name_col < len(row) else ""
            if code_val == staff["code"].upper() or staff["name"].lower() in name_val:
                matched_row = row
                break

        if matched_row is None:
            raise RuntimeError(
                f"Không tìm thấy nhân sự '{staff['name']}' ({staff['code']}) trong tab '{tab_name}'."
            )

        raw_val = matched_row[date_col].strip() if date_col < len(matched_row) else ""
        is_working = raw_val.upper() == "X"
        statuses.append(
            StaffStatus(
                code=staff["code"],
                name=staff["name"],
                account=staff["account"],
                raw_value=raw_val or "(trống)",
                is_working=is_working,
            )
        )
        if is_working:
            active_accounts.append(staff["account"])

    return AttendanceResult(
        target_date=target_date,
        date_header=date_header,
        source=source,
        tab_name=tab_name,
        staff_statuses=statuses,
        active_accounts=active_accounts,
    )


def _fetch_gviz_csv_rows(spreadsheet_id: str, tab_name: str, timeout: int = 15) -> List[List[str]]:
    """Download CSV rows for a specific tab via Google Visualization CSV endpoint."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(tab_name)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} khi tải Google Sheet tab '{tab_name}'.")
        content_type = resp.headers.get("Content-Type", "")
        raw_bytes = resp.read()
    text = raw_bytes.decode("utf-8", errors="replace")
    if "text/html" in content_type.lower() or text.lstrip().lower().startswith("<!doctype html"):
        raise RuntimeError(
            "Google Sheet yêu cầu đăng nhập (không mở xem công khai). "
            "Hãy chia sẻ quyền Xem cho Service Account hoặc dùng cờ --sales."
        )
    return list(csv.reader(io.StringIO(text)))


def _fetch_via_service_account(
    spreadsheet_id: str,
    target_date: date,
    sa_file: str,
) -> Optional[AttendanceResult]:
    """Optional fallback: read via gspread + Google Service Account JSON."""
    try:
        import gspread  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Đã cấu hình GOOGLE_SERVICE_ACCOUNT_FILE nhưng chưa cài gspread. "
            "Chạy: pip install gspread google-auth"
        ) from exc

    gc = gspread.service_account(filename=sa_file)
    sh = gc.open_by_key(spreadsheet_id)
    for tab in _tab_candidates(target_date):
        try:
            ws = sh.worksheet(tab)
        except Exception:
            continue
        rows = ws.get_all_values()
        parsed = parse_attendance_rows(rows, target_date, ws.title, source="service_account")
        if parsed is not None:
            return parsed
    return None


def get_attendance(
    target_date: Optional[date] = None,
    sales_override: Optional[str] = None,
    spreadsheet_id: str = ATTENDANCE_SPREADSHEET_ID,
) -> AttendanceResult:
    """Resolve who is working on target_date (CLI override -> Public CSV -> Service Account)."""
    dt = target_date or today_vn()
    if sales_override:
        return resolve_sales_override(sales_override, dt)

    last_err: Optional[Exception] = None
    for tab in _tab_candidates(dt):
        try:
            rows = _fetch_gviz_csv_rows(spreadsheet_id, tab)
            parsed = parse_attendance_rows(rows, dt, tab, source="google_sheets_csv")
            if parsed is not None:
                return parsed
        except Exception as exc:
            last_err = exc

    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if sa_file and os.path.exists(sa_file):
        parsed = _fetch_via_service_account(spreadsheet_id, dt, sa_file)
        if parsed is not None:
            return parsed

    detail = f" ({last_err})" if last_err else ""
    raise RuntimeError(
        f"Không đọc được lịch trực ngày {dt.strftime('%d/%m/%Y')} từ Google Sheets{detail}. "
        "Bạn có thể truyền tạm --sales Frescadorado,angie0987654321.gip@gmail.com"
    )
