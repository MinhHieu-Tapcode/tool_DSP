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

  const mode = body.mode === "apply" ? "apply" : "dry-run";
  const concNum = Number(body.concurrency);
  const concurrency = String(
    Number.isFinite(concNum) ? Math.min(8, Math.max(1, Math.trunc(concNum))) : 4
  );

  let inputs;
  if (tool.id === "fillin") {
    inputs = {
      mode,
      sales: String(body.sales || "").trim().slice(0, 120),
      date: String(body.date || "").trim().slice(0, 20),
      skip_step2: String(Boolean(body.skip_step2)),
      concurrency,
    };
  } else {
    const noCancel = body.no_cancel === undefined ? true : Boolean(body.no_cancel);
    inputs = {
      mode,
      no_cancel: String(noCancel),
      concurrency,
    };
  }

  try {
    const resp = await ghFetch(
      `/actions/workflows/${encodeURIComponent(tool.workflow)}/dispatches`,
      {
        method: "POST",
        body: { ref: "main", inputs },
      }
    );
    if (!resp.ok) {
      const txt = await resp.text();
      return sendJson(res, resp.status, {
        ok: false,
        error: `Không kích hoạt được ${tool.name}: ${txt.slice(0, 240)}`,
      });
    }
    return sendJson(res, 200, {
      ok: true,
      tool: tool.id,
      inputs,
      message: `Đã gửi lệnh chạy ${tool.name} (${mode}). Hệ thống sẽ khởi động sau vài giây.`,
    });
  } catch (err) {
    return sendJson(res, 500, { ok: false, error: err.message });
  }
};
