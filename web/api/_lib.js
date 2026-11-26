"use strict";

const crypto = require("crypto");

const COOKIE_NAME = "tool_dsp_session";
const SESSION_MAX_AGE_SEC = 7 * 24 * 60 * 60;

const TOOLS = {
  fillin: {
    id: "fillin",
    workflow: "fillin.yml",
    name: "FILL IN DATA",
    subtitle: "Check trùng SĐT, chuyển CALL >= 50%, hủy TEST/DUP/MULT & chia sale theo lịch trực",
    scheduleText: "06:00 → 14:00 (mỗi 30 phút) + chốt 14:05 VN",
  },
  gip287: {
    id: "gip287",
    workflow: "gip287.yml",
    name: "GIP 287 (WeUpBook)",
    subtitle: "Gắn NEED PAY ADVANCED (>= 60% & > 3 hoàn), check trùng 30 ngày & phân loại combo",
    scheduleText: "08:45, 11:45, 14:45 + chốt 15:05 VN",
  },
};

function getConfig() {
  const githubToken = (process.env.GITHUB_TOKEN || "").trim();
  const appPassword = process.env.APP_PASSWORD || "";
  const owner = (process.env.GITHUB_OWNER || "MinhHieu-Tapcode").trim();
  const repo = (process.env.GITHUB_REPO || "tool_DSP").trim();
  const rawSecret = (process.env.SESSION_SECRET || "").trim();
  const sessionSecret =
    rawSecret ||
    crypto
      .createHash("sha256")
      .update(`tool_dsp:${appPassword}:${githubToken}`)
      .digest("hex");

  const missingEnv = [];
  if (!appPassword) missingEnv.push("APP_PASSWORD");
  if (!githubToken) missingEnv.push("GITHUB_TOKEN");

  return {
    githubToken,
    appPassword,
    owner,
    repo,
    sessionSecret,
    missingEnv,
    configured: missingEnv.length === 0,
  };
}

function sha256(val) {
  return crypto.createHash("sha256").update(String(val || "")).digest();
}

function signToken(expUnixSec, secret) {
  const payload = String(expUnixSec);
  const sig = crypto.createHmac("sha256", secret).update(payload).digest("hex");
  return `${payload}.${sig}`;
}

function verifyPassword(inputPassword) {
  const { appPassword } = getConfig();
  if (!appPassword || typeof inputPassword !== "string") return false;
  return crypto.timingSafeEqual(sha256(inputPassword), sha256(appPassword));
}

function parseCookies(req) {
  const out = {};
  const raw = req.headers && req.headers.cookie;
  if (!raw) return out;
  for (const part of raw.split(";")) {
    const idx = part.indexOf("=");
    if (idx <= 0) continue;
    const k = part.slice(0, idx).trim();
    const v = part.slice(idx + 1).trim();
    out[k] = decodeURIComponent(v);
  }
  return out;
}

function isAuthenticated(req) {
  const cfg = getConfig();
  if (!cfg.configured) return false;
  const cookies = parseCookies(req);
  const token = cookies[COOKIE_NAME];
  if (!token || !token.includes(".")) return false;
  const [expStr, sigHex] = token.split(".", 2);
  const exp = Number(expStr);
  if (!Number.isFinite(exp) || exp * 1000 <= Date.now()) return false;
  const expected = signToken(exp, cfg.sessionSecret);
  if (!sigHex || token.length !== expected.length) return false;
  try {
    return crypto.timingSafeEqual(Buffer.from(token), Buffer.from(expected));
  } catch {
    return false;
  }
}

function buildSessionCookie(req) {
  const cfg = getConfig();
  const exp = Math.floor(Date.now() / 1000) + SESSION_MAX_AGE_SEC;
  const token = signToken(exp, cfg.sessionSecret);
  const proto = (req.headers["x-forwarded-proto"] || "").toLowerCase();
  const isSecure = proto === "https" || process.env.NODE_ENV === "production";
  const attrs = [
    `${COOKIE_NAME}=${encodeURIComponent(token)}`,
    `Max-Age=${SESSION_MAX_AGE_SEC}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
  ];
  if (isSecure) attrs.push("Secure");
  return attrs.join("; ");
}

function buildClearCookie(req) {
  const proto = (req.headers["x-forwarded-proto"] || "").toLowerCase();
  const isSecure = proto === "https" || process.env.NODE_ENV === "production";
  const attrs = [
    `${COOKIE_NAME}=`,
    "Max-Age=0",
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
  ];
  if (isSecure) attrs.push("Secure");
  return attrs.join("; ");
}

function sendJson(res, statusCode, payload) {
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(payload));
}

function requireAuth(req, res) {
  const cfg = getConfig();
  if (!cfg.configured) {
    sendJson(res, 500, {
      ok: false,
      error: `Chưa cấu hình biến môi trường trên Vercel: ${cfg.missingEnv.join(", ")}`,
    });
    return false;
  }
  if (!isAuthenticated(req)) {
    sendJson(res, 401, { ok: false, error: "Phiên đăng nhập hết hạn hoặc chưa đăng nhập." });
    return false;
  }
  return true;
}

async function parseJsonBody(req) {
  if (req.body && typeof req.body === "object") return req.body;
  if (typeof req.body === "string") {
    return req.body ? JSON.parse(req.body) : {};
  }
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1024 * 64) {
        reject(new Error("Payload too large"));
      }
    });
    req.on("end", () => {
      if (!raw.trim()) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

async function ghFetch(path, options = {}) {
  const cfg = getConfig();
  const url = path.startsWith("http")
    ? path
    : `https://api.github.com/repos/${encodeURIComponent(cfg.owner)}/${encodeURIComponent(cfg.repo)}${path}`;

  const headers = {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${cfg.githubToken}`,
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "tool-dsp-dashboard",
    ...(options.headers || {}),
  };

  const fetchOpts = { ...options, headers };
  if (fetchOpts.body && typeof fetchOpts.body === "object" && !Buffer.isBuffer(fetchOpts.body)) {
    headers["Content-Type"] = "application/json";
    fetchOpts.body = JSON.stringify(fetchOpts.body);
  }
  return fetch(url, fetchOpts);
}

async function ghJson(path, options = {}) {
  const resp = await ghFetch(path, options);
  const text = await resp.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text.slice(0, 300) };
    }
  }
  if (!resp.ok) {
    const msg = (data && data.message) || `GitHub API lỗi HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}

module.exports = {
  TOOLS,
  getConfig,
  verifyPassword,
  isAuthenticated,
  buildSessionCookie,
  buildClearCookie,
  sendJson,
  requireAuth,
  parseJsonBody,
  ghFetch,
  ghJson,
};
