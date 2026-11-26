"use strict";

const { TOOLS, ghFetch, parseJsonBody, requireAuth, sendJson } = require("./_lib");

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

  const tool = TOOLS[body.tool];
  if (!tool) {
    return sendJson(res, 400, { ok: false, error: "Tool không hợp lệ (chọn fillin hoặc gip287)." });
  }
  if (typeof body.enabled !== "boolean") {
    return sendJson(res, 400, { ok: false, error: "Thiếu trạng thái enabled (true/false)." });
  }

  const action = body.enabled ? "enable" : "disable";
  try {
    const resp = await ghFetch(
      `/actions/workflows/${encodeURIComponent(tool.workflow)}/${action}`,
      { method: "PUT" }
    );
    if (!resp.ok) {
      const txt = await resp.text();
      return sendJson(res, resp.status, {
        ok: false,
        error: `Không ${body.enabled ? "bật" : "tắt"} được ${tool.name}: ${txt.slice(0, 200)}`,
      });
    }
    return sendJson(res, 200, {
      ok: true,
      tool: tool.id,
      enabled: body.enabled,
      message: `Đã ${body.enabled ? "BẬT lịch tự động" : "TẮT lịch tự động"} cho ${tool.name}.`,
    });
  } catch (err) {
    return sendJson(res, 500, { ok: false, error: err.message });
  }
};
