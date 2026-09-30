# Changelog

## Unreleased

- Permission-aware navigation and invoice/cash document tabs now reflect user module access.
- Direct document viewing now requires the actual accounting module permission unless the user has reports access.
- Added a local verification script for compile + pytest when hosted CI is unavailable.

- Added a shareable /demo landing with explicit demo labeling, POST-only demo start, Web Share API and clipboard fallback.
- Added persistent in-app demo warning/ribbon and social link preview metadata.
- Added a dedicated transaction landing at /transactions and upgraded /reports into the advanced transaction search page.
- Transaction search now supports exact amount matching from the quick search field and applies amount bounds at database-query level.
- Finalized Paki/PWA, isolated demo safeguards, offline distribution, health monitoring and credential-at-rest protection.
- Demo share links preserve HTTPS when the app is behind the bundled reverse-proxy deployment.
- Hardened the report fallback so already-formatted Jalali dates are not re-parsed.
- CI remains blocked by a GitHub hosted-runner assignment failure; no test step has executed yet.

## 0.9.0 — 2026-09-30

- Added the interactive Paki / hp application identity.
- Added a high-contrast simplified SVG favicon.
- Added PWA manifest and a static-asset service worker.
- Added isolated demo mode with reset-on-start and cleanup-on-logout.
- Added at-rest encryption for stored AI API credentials with legacy-read migration.
- Added CI tests for Paki assets and the secret store.
- Updated deployment to provision a stable credential-encryption key.
