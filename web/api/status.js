"use strict";

const { TOOLS, getConfig, ghJson, requireAuth, sendJson } = require("./_lib");

async function loadToolStatus(tool) {
  const [wf, runsRes] = await Promise.all([
    ghJson(`/actions/workflows/${encodeURIComponent(tool.workflow)}`),
    ghJson(`/actions/workflows/${encodeURIComponent(tool.workflow)}/runs?per_page=8`),
  ]);

  const rawRuns = Array.isArray(runsRes && runsRes.workflow_runs) ? runsRes.workflow_runs : [];

  const enriched = await Promise.all(
    rawRuns.map(async (r, idx) => {
      let artifacts = [];
      let currentStep = "";

      if (idx < 5 && r.status === "completed") {
        try {
          const artData = await ghJson(`/actions/runs/${r.id}/artifacts?per_page=10`);
          artifacts = (artData.artifacts || [])
            .filter((a) => !a.expired)
            .map((a) => ({
              id: a.id,
              name: a.name,
              sizeBytes: a.size_in_bytes,
            }));
        } catch {
          artifacts = [];
        }
      }

      if (r.status === "in_progress" || r.status === "queued") {
        try {
          const jobsData = await ghJson(`/actions/runs/${r.id}/jobs?per_page=5`);
          const job = (jobsData.jobs || [])[0];
          if (job && Array.isArray(job.steps)) {
            const runningStep = job.steps.find((s) => s.status === "in_progress");
            if (runningStep) currentStep = runningStep.name;
          }
        } catch {
          currentStep = "";
        }
      }

      const startMs = Date.parse(r.run_started_at || r.created_at);
      const endMs = r.status === "completed" ? Date.parse(r.updated_at) : Date.now();
      const durationSec =
        Number.isFinite(startMs) && Number.isFinite(endMs) && endMs >= startMs
          ? Math.round((endMs - startMs) / 1000)
          : null;

      return {
        id: r.id,
        runNumber: r.run_number,
        event: r.event,
        status: r.status,
        conclusion: r.conclusion,
        createdAt: r.created_at,
        startedAt: r.run_started_at || r.created_at,
        updatedAt: r.updated_at,
        durationSec,
        currentStep,
        artifacts,
        htmlUrl: r.html_url,
      };
    })
  );

  return {
    ...tool,
    state: wf.state,
    enabled: wf.state === "active",
    runs: enriched,
  };
}

module.exports = async (req, res) => {
  if (req.method !== "GET") {
    return sendJson(res, 405, { ok: false, error: "Method not allowed" });
  }
  if (!requireAuth(req, res)) return;

  try {
    const cfg = getConfig();
    const [fillin, gip287] = await Promise.all([
      loadToolStatus(TOOLS.fillin),
      loadToolStatus(TOOLS.gip287),
    ]);
    return sendJson(res, 200, {
      ok: true,
      serverTime: new Date().toISOString(),
      repo: `${cfg.owner}/${cfg.repo}`,
      tools: { fillin, gip287 },
    });
  } catch (err) {
    return sendJson(res, err.status || 500, {
      ok: false,
      error: `Không lấy được trạng thái từ GitHub: ${err.message}`,
    });
  }
};
