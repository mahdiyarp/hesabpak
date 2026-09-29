# Changelog

## Unreleased

- Finalized Paki/PWA, isolated demo safeguards, offline distribution, health monitoring and credential-at-rest protection.
- CI remains blocked by a GitHub hosted-runner assignment failure; no test step has executed yet.

## 0.9.0 — 2026-09-30

- Added the interactive Paki / hp application identity.
- Added a high-contrast simplified SVG favicon.
- Added PWA manifest and a static-asset service worker.
- Added isolated demo mode with reset-on-start and cleanup-on-logout.
- Added at-rest encryption for stored AI API credentials with legacy-read migration.
- Added CI tests for Paki assets and the secret store.
- Updated deployment to provision a stable credential-encryption key.
