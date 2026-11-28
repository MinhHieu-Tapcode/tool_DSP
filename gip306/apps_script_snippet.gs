// Dán đoạn mã này vào Extensions -> Apps Script trên file "GIP 306 - DATA NEW",
// rồi chọn Deploy -> New deployment -> Web app (Execute as: Me, Who has access: Anyone).
// Lấy Web App URL dán vào biến GIP306_SHEET_WEBHOOK_URL trong file .env.

const WEBHOOK_SECRET = "gip306_luong1_2026"; // Đổi nếu muốn, khớp với GIP306_SHEET_WEBHOOK_SECRET trong .env

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents || "{}");
    if (WEBHOOK_SECRET && body.secret !== WEBHOOK_SECRET) {
      return ContentService.createTextOutput(JSON.stringify({ ok: false, error: "Unauthorized" }))
        .setMimeType(ContentService.MimeType.JSON);
    }
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = ss.getSheetByName(body.sheet_name || "sheet");
    if (!sheet) {
      throw new Error("Khong tim thay tab: " + (body.sheet_name || "sheet"));
    }
    const maxRows = Math.max(sheet.getLastRow(), 2);
    if (maxRows >= 2) {
      sheet.getRange(2, 1, maxRows - 1, 3).clearContent();
    }
    const rows = Array.isArray(body.rows) ? body.rows : [];
    if (rows.length > 0) {
      const normalized = rows.map(r => [r[0] || "", r[1] || "", r[2] || ""]);
      sheet.getRange(2, 1, normalized.length, 3).setValues(normalized);
    }
    return ContentService.createTextOutput(JSON.stringify({ ok: true, written: rows.length }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
