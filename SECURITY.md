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
- **Audio and transcripts leave the machine** — recorded answers are uploaded to the
  HuggingFace Inference API for transcription and grading. Do not speak credentials,
  personal data or anything confidential into an answer.
- **Logs** — `logs/` is gitignored and deliberately records only latency and scores, never
  transcripts or audio. Keep it that way; a log of what candidates said is personal data.
- **Auth is a placeholder** — the login page accepts any password and holds no session
  server-side. Do not expose this server to an untrusted network, and do not treat the
  candidate name as authenticated.
- **Binds to 0.0.0.0 by default** — `run()` listens on all interfaces, so anyone on your
  network can reach it and spend your HF credits. Pass `host="127.0.0.1"` if that matters.
- **Supply chain** — runtime dependencies are kept minimal and monitored via Dependabot and
  pip-audit.

## Non-Goals

- placement-practice does not authenticate users, isolate one candidate's data from another's,
  or persist reports. It is a local practice tool, not a proctored assessment platform.
- placement-practice does not guarantee the confidentiality of audio or transcripts once
  they reach the inference provider — that is governed by the provider's terms.
- Scores come from an LLM judge at non-zero temperature and are not reproducible or
  defensible as a hiring signal.
