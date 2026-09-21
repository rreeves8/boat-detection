// Login-only Worker on login.magnusreeves.com.
//
// It does not serve or proxy any content — Google Cloud CDN serves the private
// bucket at boats.magnusreeves.com and enforces access with a signed cookie.
// This Worker's only job: check the passphrase, mint that Cloud CDN signed
// cookie (HMAC-SHA1 over the policy with the signing key), set it for the whole
// parent domain, and bounce the visitor to the footage.
//
// Secrets (wrangler): SHARED_PASSWORD, CDN_KEY_NAME, CDN_KEY_VALUE (base64url).

const SITE = "https://boats.magnusreeves.com";
const URL_PREFIX = SITE + "/"; // the signed cookie authorizes this prefix
const COOKIE_TTL = 60 * 60 * 24 * 30; // 30 days

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/logout") return logout();
    if (request.method !== "POST") return htmlResponse(loginPage());

    const form = await request.formData();
    if (!timingSafeEqual(form.get("password") || "", env.SHARED_PASSWORD)) {
      return htmlResponse(loginPage("Wrong passphrase."), 401);
    }

    const cookie = await mintCdnCookie(env);
    const headers = new Headers({ Location: SITE + "/data" });
    headers.append(
      "Set-Cookie",
      // Domain is the registrable parent so the cookie is sent to boats.* (the
      // LB) even though it's set here on login.*.
      `Cloud-CDN-Cookie=${cookie}; Domain=magnusreeves.com; Path=/; Max-Age=${COOKIE_TTL}; Secure; HttpOnly; SameSite=Lax`,
    );
    return new Response(null, { status: 302, headers });
  },
};

// ---- Cloud CDN signed cookie ------------------------------------------------

async function mintCdnCookie(env) {
  const expires = Math.floor(Date.now() / 1000) + COOKIE_TTL;
  const policy =
    `URLPrefix=${b64url(URL_PREFIX)}:Expires=${expires}:KeyName=${env.CDN_KEY_NAME}`;
  const keyBytes = b64urlDecode(env.CDN_KEY_VALUE);
  const key = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-1" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(policy));
  return `${policy}:Signature=${b64url(sig)}`;
}

// ---- helpers ----------------------------------------------------------------

function b64url(input) {
  const bytes = typeof input === "string" ? new TextEncoder().encode(input) : new Uint8Array(input);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(str) {
  const b64 = str.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

function timingSafeEqual(a, b) {
  a = String(a);
  b = String(b);
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function logout() {
  const headers = new Headers({ Location: SITE + "/" });
  headers.append(
    "Set-Cookie",
    "Cloud-CDN-Cookie=; Domain=magnusreeves.com; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax",
  );
  return new Response(null, { status: 302, headers });
}

const htmlResponse = (html, status = 200) =>
  new Response(html, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });

function loginPage(error) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Enter passphrase</title>
<style>
  :root{color-scheme:dark;font-family:Inter,system-ui,sans-serif;background:#101315;color:#edf1ee}
  body{margin:0;display:grid;place-items:center;min-height:100vh}
  form{background:#171d1a;border:1px solid #262f29;border-radius:12px;padding:28px;width:min(360px,90vw)}
  h1{font-size:18px;font-weight:500;margin:0 0 4px}
  p{color:#97a29b;font-size:13px;margin:0 0 18px}
  input{width:100%;box-sizing:border-box;background:#101315;border:1px solid #2a332e;border-radius:6px;
    color:#edf1ee;padding:11px 12px;font-size:14px}
  button{margin-top:12px;width:100%;background:#b7e0c7;color:#15291d;border:0;border-radius:6px;
    padding:11px;font-weight:600;cursor:pointer}
  .err{color:#e6997f;font-size:12px;margin-top:10px}
  a{color:#b7e0c7;font-size:12px}
</style></head><body>
<form method="POST" action="/">
  <h1>Stills Bay footage</h1>
  <p>Enter the passphrase to view the footage archive.</p>
  <input type="password" name="password" placeholder="Passphrase" autofocus autocomplete="current-password"/>
  <button type="submit">Enter</button>
  ${error ? `<div class="err">${error}</div>` : ""}
  <div style="margin-top:14px"><a href="${SITE}/">&larr; Back to overview</a></div>
</form></body></html>`;
}
