"use strict";

const { ghFetch, parseJsonBody, requireAuth, sendJson } = require("./_lib");

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    return sendJson(res, 405, { ok: false, error: "Method not allowed" });
  }
  if (!requireAuth(req, res)) return;

  let body;
  try {
    body = await parseJsonBody(req);
  } catch {
    return sendJson(res, 400, { ok: false, error: "Body JSON không hợp lệ." });
  }

  const runId = String(body.runId || "").trim();
  if (!/^\d+$/.test(runId)) {
    return sendJson(res, 400, { ok: false, error: "runId không hợp lệ." });
  }

  try {
    const resp = await ghFetch(`/actions/runs/${runId}/cancel`, { method: "POST" });
    if (!resp.ok) {
      const txt = await resp.text();
      return sendJson(res, resp.status, {
        ok: false,
        error: `Không dừng được phiên #${runId}: ${txt.slice(0, 200)}`,
      });
    }
    return sendJson(res, 200, {
      ok: true,
      message: `Đã gửi lệnh dừng phiên #${runId}.`,
    });
  } catch (err) {
    return sendJson(res, 500, { ok: false, error: err.message });
  }
};
