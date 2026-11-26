# gsolution-auto — Tool Check Trùng & Chia Data Tự Động (0 Token)

Tool Python + Playwright chạy ngầm dưới terminal (headless Chrome), tự động thực hiện trọn bộ quy trình **Bước 1 → Bước 5** trên `g-solution.vn` và xuất báo cáo Excel/JSON chi tiết.

## 1. Cấu trúc thư mục
- `run.py` — Lệnh chạy chính (`--dry-run` mặc định hoặc `--apply`)
- `attendance.py` — Bước 1: đọc lịch trực từ Google Sheets (tab tháng hiện tại) hoặc ghi đè bằng `--sales`
- `classifier.py` — Bộ rule phân loại đơn (`CALL >= 50%`, `TEST > DUPLICATE (cùng GIP, 30 ngày) > MULTIPLE RETURNED (>=3) > DIFFERENT PRODUCT > RE ORDER`)
- `browser_client.py` — Điều khiển Playwright headless (song song 4 tab khi tra lịch sử SĐT, tự xử lý lỗi tìm kiếm `+63`)
- `reporter.py` — Xuất báo cáo Excel (`.xlsx`), JSON (`.json`), log và thông báo Telegram tùy chọn
- `run_background.ps1` — Script chạy ngầm / đăng ký lịch Windows Task Scheduler
- `.env` — Lưu tài khoản đăng nhập cục bộ (tuyệt đối không chia sẻ ra ngoài)

---

## 2. Cách chạy trên Terminal

Mở PowerShell tại thư mục `C:\Users\Hieu\.gemini\antigravity\scratch\gsolution-auto`:

### Chế độ kiểm tra chỉ đọc (`--dry-run` — mặc định, chưa sửa/hủy gì)
```powershell
python run.py --dry-run
```
Test nhanh giới hạn 6 SĐT trước:
```powershell
python run.py --dry-run --max-phones 6 --concurrency 3
```

### Chế độ thực thi thật (`--apply` — chạy ngầm toàn bộ)
```powershell
python run.py --apply
```
- **Lưu ý an toàn:** Các đơn rơi vào nhóm `AMBIGUOUS` (ca biên không đủ dữ liệu đối chiếu) sẽ **tự động bỏ qua** không hủy/chia bừa và tách riêng vào sheet `Ambiguous_Review` trong file Excel.

### Ghi đè danh sách sale nhận đơn (`--sales`)
```powershell
# Chỉ chia cho Fresca
python run.py --apply --sales Frescadorado

# Chia cho cả 2 bạn (bỏ qua đọc Google Sheets)
python run.py --apply --sales Frescadorado,angie0987654321.gip@gmail.com
```

### Các cờ hữu ích khác
- `--mode fillin|gip287` : Chọn luồng `fillin` (mặc định: FILL IN DATA) hoặc `gip287` (GIP 287 - WeUpBook)
- `--headed` : Hiện cửa sổ Chrome để xem trực tiếp lúc chạy
- `--concurrency 5` : Chỉnh số tab tra lịch sử SĐT chạy song song (mặc định `4`)
- `--date 24/11/2026` : Chạy theo một ngày cụ thể trên lịch trực
- `--skip-step2` : Bỏ qua Bước 2 (3 GIP hoàn ≥ 50%)
- `--notify-telegram` : Gửi tóm tắt qua Telegram (sau khi điền `TELEGRAM_BOT_TOKEN` và `TELEGRAM_CHAT_ID` trong `.env`)

---

## 3. Luồng riêng `GIP 287` (`--mode gip287`)

- **Pha 1 (`/admin/order`):** Lọc `408023150 - GIP 287`, tab `New`; với các đơn có `Return Rate >= 60%` và `Returned > 3` (`>= 4`), gắn tag `ORDER STATUS / NEED PAY ADVANCED` (không đổi Service Type, không áp `MULTIPLE RETURNED`).
- **Pha 2 (`/admin/order/telesale`):** Lọc `408023150 - GIP 287` + `Duplicated Phone`, tab `New`. So lịch sử cùng `GIP 287` trong **30 ngày** (bỏ qua `Canceled` và GIP khác), không `Assign sale`:
  - `TEST` -> hủy lý do `Test`.
  - Trùng 100% (`TH2`) hoặc đơn nhỏ nằm gọn trong đơn lớn (`TH3`) -> hủy đơn nhỏ/mới lý do `Duplicate` (đơn nhỏ cũ được phép hủy đến trạng thái `Pending`; quá `Pending` chuyển `REVIEW`).
  - Trùng một phần giữa các đơn đang chạy -> gắn `MULTIPLE ORDERS`; khác hẳn / Book vs Ebook / đối chiếu đơn đã đạt trạng thái cuối (`Delivered`/`Returning`/`Returned`) trùng `< 50%` -> gắn `DIFFERENT PRODUCT`; ca cần gỡ SP combo hoặc thiếu mã -> tách `Ambiguous_Review`.
- Chạy kiểm tra chỉ đọc: `python run.py --mode gip287 --dry-run`
- Chạy thực thi thật: `python run.py --mode gip287 --apply`

---

## 4. Tự động chạy ngầm theo giờ (Windows Task Scheduler)

- **`GSolutionAutoFillIn` (`fillin`):** mỗi 30 phút `06:00` → `14:00` + chốt `14:05` (cut-off `14:00`):
```powershell
.\run_background.ps1 -RegisterTask
```

- **`GSolutionAutoGip287` (`gip287`):** mỗi 3 tiếng `08:45`, `11:45`, `14:45` + chốt `15:05` (cut-off `15:00`):
```powershell
.\run_background.ps1 -Mode gip287 -RegisterTask
```

Tùy chỉnh khung giờ riêng (ví dụ `08:30` và `13:30`):
```powershell
.\run_background.ps1 -RegisterTask -ScheduleTimes "08:30","13:30"
```

Chạy kiểm thử ngầm 1 lần qua PowerShell wrapper:
```powershell
.\run_background.ps1                 # fillin --dry-run
.\run_background.ps1 -Apply          # fillin --apply
.\run_background.ps1 -Mode gip287    # gip287 --dry-run
```

---

## 5. Chạy Online qua GitHub Actions (Không cần bật máy)

Repo đã tích hợp sẵn 2 workflow trong `.github/workflows/`:
- **`.github/workflows/fillin.yml` (`Fill-In Data Auto`):** tự chạy `--apply` từ `06:00 → 14:00` (mỗi 30 phút) + `14:05` giờ VN.
- **`.github/workflows/gip287.yml` (`GIP 287 Auto`):** tự chạy `--apply --no-cancel` lúc `08:45`, `11:45`, `14:45`, `15:05` giờ VN.

### Thiết lập lần đầu
1. Đảm bảo repo GitHub ở chế độ **Private** (**Settings → General → Danger Zone**).
2. Vào **Settings → Secrets and variables → Actions → New repository secret**, thêm:
   - `GSOLUTION_EMAIL`: Email đăng nhập `g-solution.vn`
   - `GSOLUTION_PASSWORD`: Mật khẩu `g-solution.vn`
   - *(Tùy chọn)* `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Nhận tóm tắt qua Telegram.
3. Cấp quyền cho người làm thay tại **Settings → Collaborators → Add people** (quyền `Write`).

### Cách người làm thay sử dụng
1. Mở tab **Actions** trên GitHub → chọn **`Fill-In Data Auto`** hoặc **`GIP 287 Auto`**.
2. Muốn chạy ngay → bấm **`Run workflow`** → chọn `dry-run` (test) hoặc `apply` (chạy thật), có thể điền `sales` (vd: `Frescadorado`) nếu cần ghi đè lịch trực.
3. Sau khi chạy xong, vào chi tiết lượt chạy → tải file `.zip` ở mục **Artifacts** để xem báo cáo Excel/JSON và log.
4. *(Quan trọng)* Test thử 1 lượt `dry-run` trên GitHub thành công xong hãy **Disable** lịch chạy trên máy Windows cá nhân để tránh hai nơi chạy trùng nhau.

---

## 6. Chạy Unit Test kiểm tra Rule
```powershell
python -m unittest discover -s . -v
```
