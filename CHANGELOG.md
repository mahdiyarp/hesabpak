# Changelog

## Unreleased

- Invoice viewing is now a print-friendly RTL document with browser print / Save-to-PDF support and consistent grouped amounts.
- Invoice-prefill data in receive/payment forms is now restricted by matching invoice kind and module/report permission.
- Verification now also checks required runtime files and Bash syntax for all project shell scripts.
- Cash-document edit forms now use the same grouped amount formatting as other accounting forms.
- Offline bundles preserve their source commit and expose it through `/healthz` after `.git` removal.
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
- Hardened Passenger/host update helpers so backups happen before code changes and legacy update paths no longer ignore dependency or restart failures.
- Added runtime Git commit identity to /healthz for live-version verification.
- CI remains blocked by a GitHub hosted-runner assignment failure; no test step has executed yet.
