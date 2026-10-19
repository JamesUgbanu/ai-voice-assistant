# Security policy

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting / Security Advisory** for
this repository. Do not open a public issue for a suspected vulnerability.

Include the affected endpoint or file, reproduction steps, and impact. Do not
include real credentials, audio recordings, transcripts, or visitor data.

## Scope

- The service's HTTP/WebSocket endpoints, authentication tickets, origin
  validation, quotas, session isolation, and audio/message bounds.
- Configuration validation and secret handling.
- The generic HTTP responder boundary and the portfolio adapter contract.
- Privacy of uploaded audio and generated transcripts.

## Handling sensitive data

- Secrets belong in `.env` (Git-ignored) or a secret manager, never in code,
  tests, logs, or issues.
- Private voice references and diagnostic captures are Git-ignored and must not
  be committed or attached to reports.
- If a secret was ever committed, removing it from the working tree is not
  enough: rotate it and require a history rewrite before publishing. Do not
  rewrite history without the repository owner's authorization.

## Supported versions

Security fixes target the `main` branch. Released images should be pinned by
digest; see `DEPLOY.md`.
