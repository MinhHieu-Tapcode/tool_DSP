"use strict";

const {
  buildClearCookie,
  buildSessionCookie,
  getConfig,
  isAuthenticated,
  parseJsonBody,
  sendJson,
  verifyPassword,
} = require("./_lib");

module.exports = async (req, res) => {
  const cfg = getConfig();

  if (req.method === "GET") {
    return sendJson(res, 200, {
      ok: true,
      configured: cfg.configured,
      missingEnv: cfg.missingEnv,
      authenticated: isAuthenticated(req),
      repo: `${cfg.owner}/${cfg.repo}`,
    });
  }

  if (req.method === "POST") {
    if (!cfg.configured) {
      return sendJson(res, 500, {
        ok: false,
        error: `Thiếu biến môi trường trên Vercel: ${cfg.missingEnv.join(", ")}`,
      });
    }
    let body;
    try {
      body = await parseJsonBody(req);
    } catch {
      return sendJson(res, 400, { ok: false, error: "Dữ liệu gửi lên không hợp lệ." });
    }
    if (!verifyPassword(body.password)) {
      return sendJson(res, 401, { ok: false, error: "Sai mật khẩu truy cập." });
    }
    res.setHeader("Set-Cookie", buildSessionCookie(req));
    return sendJson(res, 200, { ok: true, authenticated: true });
  }

  if (req.method === "DELETE") {
    res.setHeader("Set-Cookie", buildClearCookie(req));
    return sendJson(res, 200, { ok: true, authenticated: false });
  }

  return sendJson(res, 405, { ok: false, error: "Method not allowed" });
};
