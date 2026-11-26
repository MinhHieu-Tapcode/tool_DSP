"use strict";

const { ghFetch, ghJson, requireAuth, sendJson } = require("./_lib");

const ANSI_RE = /\x1b\[[0-9;]*[A-Za-z]/g;

module.exports = async (req, res) => {
  if (req.method !== "GET") {
    return sendJson(res, 405, { ok: false, error: "Method not allowed" });
  }
  if (!requireAuth(req, res)) return;

  const runId = String((req.query && req.query.runId) || "").trim();
  if (!/^\d+$/.test(runId)) {
    return sendJson(res, 400, { ok: false, error: "Thiếu hoặc sai runId." });
  }

  try {
    const jobsData = await ghJson(`/actions/runs/${runId}/jobs?per_page=10`);
    const jobs = Array.isArray(jobsData.jobs) ? jobsData.jobs : [];
    const job =
      jobs.find((j) => /fillin|gip287/i.test(j.name || "")) || jobs[0];

    if (!job) {
      return sendJson(res, 200, {
        ok: true,
        jobName: "-",
        status: "queued",
        conclusion: null,
        lines: ["Phiên chạy đang xếp hàng khởi tạo trên GitHub Actions, chưa có log chi tiết."],
      });
    }

    const logResp = await ghFetch(`/actions/jobs/${job.id}/logs`, {
      redirect: "manual",
    });

    let rawLog = "";
    if (logResp.status >= 300 && logResp.status < 400) {
      const loc = logResp.headers.get("location");
      if (loc) {
        const blob = await fetch(loc);
        if (blob.ok) rawLog = await blob.text();
      }
    } else if (logResp.ok) {
      rawLog = await logResp.text();
    }

    if (!rawLog) {
      const stepsSummary = (job.steps || []).map(
        (s) => `[${s.status}${s.conclusion ? "/" + s.conclusion : ""}] ${s.name}`
      );
      return sendJson(res, 200, {
        ok: true,
        jobName: job.name,
        status: job.status,
        conclusion: job.conclusion,
        lines: stepsSummary.length
          ? stepsSummary
          : ["Log chi tiết chưa sẵn sàng (GitHub chỉ cấp log đầy đủ sau khi job xong bước hiện tại)."],
      });
    }

    const cleaned = rawLog
      .replace(ANSI_RE, "")
      .split(/\r?\n/)
      .map((ln) => ln.replace(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*/, ""));

    const toolLines = cleaned.filter(
      (ln) =>
        /\[(INFO|WARNING|ERROR)\]/.test(ln) ||
        ln.includes("===") ||
        ln.includes("Traceback") ||
        ln.includes("Error") ||
        ln.includes("FAILED") ||
        ln.includes("Ran ") ||
        ln.includes("File ")
    );

    const displayLines = (toolLines.length >= 4 ? toolLines : cleaned).slice(-140);
    return sendJson(res, 200, {
      ok: true,
      jobName: job.name,
      status: job.status,
      conclusion: job.conclusion,
      lines: displayLines,
    });
  } catch (err) {
    return sendJson(res, err.status || 500, {
      ok: false,
      error: `Không đọc được log: ${err.message}`,
    });
  }
};
