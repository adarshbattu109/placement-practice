# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability in placement-practice, please report it responsibly:

1. **Do NOT open a public issue.**
2. Use [GitHub Private Vulnerability Reporting](https://github.com/adarshbattu109/placement-practice/security/advisories/new) to submit a report.
3. Include: description, reproduction steps, impact assessment, and suggested fix (if any).

### Response Timeline

- **Acknowledgment:** Within 72 hours
- **Assessment:** Within 7 days
- **Fix/Disclosure:** Within 90 days (coordinated disclosure)

## Scope

placement-practice runs a local web server that captures microphone audio and forwards it to
a third-party inference API. We take the following seriously:

- **HuggingFace token exposure** — `HF_TOKEN` is read from `.env.local`, which is gitignored.
  Never commit it, never paste it into an issue, and revoke it at
  [hf.co/settings/tokens](https://huggingface.co/settings/tokens) if it leaks. The token is
  a bearer credential for your HF account, not just this app.
- **`SECRET_KEY` is a signing key** — it lives in the same gitignored `.env.local` and has no
  default, because a guessable key lets anyone forge a session cookie for any account.
  Rotating it invalidates every existing session.
- **`data/practice.db` holds real accounts and past transcripts** — email addresses, scrypt
  password hashes, and every answer a candidate has given. It is gitignored, and
  `tests/test_packaging.py` fails the build if it reaches a wheel. Don't attach it to an issue.
  Deleting a user cascades to their answers, their recordings and their queued jobs. Audio
  expires on its own (below); transcripts and scores do not. A 12-month archival window for
  whole sittings is specified for slice 8 and is **not** implemented yet — see the design
  doc. ponytail: no retention window on transcripts, add one if this is ever multi-tenant.
- **Audio and transcripts leave the machine** — recorded answers are uploaded to the
  HuggingFace Inference API for transcription and grading. Do not speak credentials,
  personal data or anything confidential into an answer.
- **Recordings are also kept locally for 10 days** — each take is transcoded to Opus and
  stored in `data/practice.db` so a wrong transcript can be challenged against the original
  and ASR re-run without a re-recording. `RECORDING_RETENTION_DAYS` in
  `src/constants/constants.py` is the window; the grading worker sweeps hourly. Deleting a
  user or an assessment cascades to its audio. Transcripts and scores are **not** on that
  clock — they are the product, and they persist.
- **Logs** — `logs/` is gitignored and deliberately records only latency and scores, never
  transcripts or audio. Keep it that way; a log of what candidates said is personal data.
- **Auth is real but minimal** — passwords are hashed with `scrypt` and sessions are
  `hmac`-signed cookies (`HttpOnly`, `SameSite=Lax`). But **signup is open to anyone who
  can reach the server**, there is no rate limiting on login, no password reset, no email
  verification, and no per-session revocation. The cookie omits `Secure` so it works over
  plain `http://localhost` — set it before serving over a network.
- **Binds to 0.0.0.0 by default** — `run()` listens on all interfaces, so anyone on your
  network can reach it and spend your HF credits. Pass `host="127.0.0.1"` if that matters.
- **Supply chain** — runtime dependencies are kept minimal and monitored via Dependabot and
  pip-audit.

## Non-Goals

- placement-practice does not rate-limit login, verify email addresses or reset passwords. It
  is a local practice tool, not a proctored assessment platform.
- placement-practice does not guarantee the confidentiality of audio or transcripts once
  they reach the inference provider — that is governed by the provider's terms.
- Scores come from an LLM judge at non-zero temperature and are not reproducible or
  defensible as a hiring signal.
