"use strict";

const { ghFetch, requireAuth, sendJson } = require("./_lib");

module.exports = async (req, res) => {
  if (req.method !== "GET") {
    return sendJson(res, 405, { ok: false, error: "Method not allowed" });
  }
  if (!requireAuth(req, res)) return;

  const artifactId = String((req.query && req.query.id) || "").trim();
  if (!/^\d+$/.test(artifactId)) {
    return sendJson(res, 400, { ok: false, error: "Thiếu hoặc sai mã artifact id." });
  }

  const rawName = String((req.query && req.query.name) || `report-${artifactId}`);
  const safeName = rawName.replace(/[^a-zA-Z0-9._-]+/g, "_") || `report-${artifactId}`;

  try {
    const metaResp = await ghFetch(`/actions/artifacts/${artifactId}/zip`, {
      redirect: "manual",
    });

    let blobResp;
    if (metaResp.status >= 300 && metaResp.status < 400) {
      const loc = metaResp.headers.get("location");
      if (!loc) {
        return sendJson(res, 502, { ok: false, error: "GitHub không trả về link tải artifact." });
      }
      // Do NOT send GitHub Authorization header to signed Azure/S3 blob URL
      blobResp = await fetch(loc);
    } else if (metaResp.ok) {
      blobResp = metaResp;
    } else {
      const errTxt = await metaResp.text();
      return sendJson(res, metaResp.status, {
        ok: false,
        error: `Không tải được artifact #${artifactId}: ${errTxt.slice(0, 200)}`,
      });
    }

    if (!blobResp.ok) {
      return sendJson(res, blobResp.status, {
        ok: false,
        error: `Tải file từ kho lưu trữ thất bại (HTTP ${blobResp.status}).`,
      });
    }

    const buf = Buffer.from(await blobResp.arrayBuffer());
    res.statusCode = 200;
    res.setHeader("Content-Type", "application/zip");
    res.setHeader("Content-Disposition", `attachment; filename="${safeName}.zip"`);
    res.setHeader("Content-Length", String(buf.length));
    res.setHeader("Cache-Control", "no-store");
    return res.end(buf);
  } catch (err) {
    return sendJson(res, 500, { ok: false, error: err.message });
  }
};
