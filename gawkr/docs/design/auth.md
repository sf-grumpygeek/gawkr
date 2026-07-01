# Design: authentication (roadmap #2)

Status: proposed. Depends on #1 (settings) — the browser-editable settings
endpoint is what makes an unauthenticated UI genuinely dangerous.

## What auth is (and isn't) for here

gawkr is **single-tenant, self-hosted**: one operator or a household, on a LAN
(and maybe exposed via a reverse proxy). The job is to keep other people on the
network — or the internet, if exposed — from viewing footage and changing
config. It is **not** a multi-user SaaS.

So the need is modest and the anti-goal is strong:

> **Do NOT build a user/account system.** No users table, no roles, no
> registration, no password-reset flows, no hand-rolled OAuth. That is a login
> system's worth of attack surface for a tool that has exactly one trust level.
> Rolling your own session/credential handling is how a security tool earns a CVE.

When auth is enabled it protects the **whole app** (read and write). Don't build
a per-endpoint public/private split — that's complexity plus a footgun (forget
to protect one route). Whole-app auth, one gate.

## Two supported paths

### Recommended (if you already run a reverse proxy): forward-auth

gawkr does **not** depend on any reverse proxy. But if you already run one — as
many self-hosters do — the cleanest option is to authenticate at the proxy,
before requests reach gawkr, so gawkr writes **zero** auth code. This works with
any proxy (Traefik, Caddy, nginx, HAProxy, Nginx Proxy Manager, …); pick
whatever you use. Options, documented in the README as examples, not
requirements:

- Basic auth / an access list at the proxy — simplest, built into most proxies.
- A forward-auth provider (Authelia / Authentik / tinyauth) — SSO + 2FA for
  people who already run one.

If you don't run a reverse proxy, use the built-in app password below instead —
it needs no external anything.

gawkr's only responsibility here is a deployment note:

- **Don't publish the container port to the host** — expose gawkr only on the
  proxy's network, so nobody on the LAN can hit it directly and bypass the proxy.
- **Optional belt-and-suspenders:** `TRUST_PROXY_AUTH=true` plus a shared secret
  header the proxy injects (`X-Gawkr-Proxy: <secret>`); gawkr rejects requests
  lacking it, so direct-to-container access fails even if the port leaks. The
  shared secret is env-only.

### Built-in fallback: a single app password

For operators without a reverse proxy. gawkr serves a login page and a signed
session cookie. Minimal and self-contained:

- **`APP_PASSWORD`** (env, secret — stays env-only like every other secret). If
  **unset, auth is disabled**, and the app logs a loud startup warning:
  "running without auth — anyone who can reach this can view events and change
  settings; set APP_PASSWORD or put gawkr behind an authenticating proxy."
- `POST /login` compares the submitted password to `APP_PASSWORD` with
  **`hmac.compare_digest`** (constant-time — never `==`), then sets a signed,
  **HttpOnly**, **SameSite=Lax** cookie carrying an expiry. `Secure` when served
  over HTTPS.
- A signing key, **`SESSION_SECRET`** (env). If unset, generate one and persist
  it to the data dir; **never** ship a hardcoded default — a default signing key
  makes every deployment's cookies forgeable.
- Middleware: unauthenticated requests → redirect to `/login` (HTML) or `401`
  (`/api`). `POST /logout` clears the cookie.
- **Throttle failed logins** (a short delay, optional lockout) — a single shared
  password is brute-forceable.

Use the web framework's / stdlib's session + signing tooling (e.g. itsdangerous
or Starlette's `SessionMiddleware`); do not implement cookie signing by hand.

## Secrets stay env-only (consistent with the rest of the design)

`APP_PASSWORD`, `SESSION_SECRET`, and any proxy shared-secret are **secrets** —
env-only, never in the settings table, never surfaced in the UI, never logged.
Same boundary as `UFP_API_KEY` and friends.

## Ordering

Build after #1. The settings work should land its `PUT /api/settings` as
**known-unprotected**; auth is the immediate next step that closes that gap. If
building the in-app path, the single app password is the self-contained piece;
forward-auth needs almost no gawkr code (just the deploy note + optional trust
header).

## Password rotation & lockout recovery (no reset system needed)

"I forgot the password" is already solved by how the password is stored, and it
does **not** require a reset flow. In a single-password tool there is no email,
no reset token, no "forgot password" link, no account enumeration — all of that
exists in multi-user systems to prove *which* user you are over an untrusted
channel. gawkr has one password and one trust level, so the recovery channel is
simply **infrastructure access**: whoever can edit the deployment is the admin by
definition.

Recovery path (document this in the README):

- `APP_PASSWORD` lives in env (Portainer's env box / `.env`). Forgot it or think
  it leaked? Don't recover it — **replace** it: edit the env var, redeploy. New
  password in ~30 seconds, old one irrelevant. That's the entire reset flow, and
  it's *safer* than a reset feature because there's no reset-token surface to get
  wrong.

Required behavior so rotation is clean:

- **Changing `APP_PASSWORD` must invalidate existing sessions.** If you rotate
  because of a suspected leak, old signed cookies must stop working. Implement by
  mixing the current password (e.g. its hash) into the session-signing key, or by
  a stored "session epoch" that a password change bumps. Result: a password
  change also logs everyone out — exactly what a reset should do.

Do **not** build an in-app "reset password" form. A UI that changes the password
immediately drags in "you must be logged in to use it… unless you're locked
out… so now I need a recovery path…" — i.e. reset tokens and the whole mess this
design avoids. Env-var rotation is the recovery path; infrastructure access is
the proof of ownership.

## Explicitly out of scope (single-tenant, for now)

gawkr is **not** multi-tenant. No multiple users, no admin/user role split, no
per-user accounts. This is a deliberate, current decision — revisit only as an
eyes-open scope change if gawkr ever becomes a shared platform, not as a feature
bolt-on. Until then, if delegated/revocable access is ever needed, the cheap
answer is a small set of **named revocable app tokens** (revocable per person, like
API tokens elsewhere), plus an **auth event log** (timestamped login/logout/failure
lines) for visibility — neither of which is a user system.

## Review checklist (reject a proposal that does any of these)

- Adds a users table, roles, registration, or password-reset flow.
- Rolls its own OAuth / cookie signing instead of using vetted tooling.
- Compares the password with `==` instead of a constant-time compare.
- Ships a hardcoded or committed default `SESSION_SECRET`.
- Sets a session cookie without HttpOnly (and without SameSite).
- Stores `APP_PASSWORD` / `SESSION_SECRET` in the DB or surfaces them in the UI.
- Protects only some endpoints instead of gating the whole app.
- Makes forward-auth safe only while also publishing the container port to the
  host (direct-access bypass).
- Builds an in-app "reset password" form or any password-reset-token flow.
- Adds multiple users / admin-vs-user roles (out of scope; single-tenant).
- Lets a password change leave old sessions valid (rotation must log everyone out).
