# Security policy

## Supported versions

The latest published version on PyPI is the only supported version. Bug fixes ship as patch releases; please upgrade rather than rely on a pinned older version.

## Reporting a vulnerability

If you find a security issue, please email harry.vass@gmail.com or open a private security advisory at https://github.com/Bigred97/au-weather-mcp/security/advisories/new.

Please **do not** open a public issue for security problems.

## What counts as a security issue

- **In scope**: anything that lets a malicious caller exfiltrate data they shouldn't see, escape sandbox boundaries, inject content into upstream Open-Meteo requests, or cause persistent state corruption.
- **Out of scope**: incorrect weather values (those are data-quality bugs — open a normal issue), Open-Meteo's own infrastructure issues (please report to them directly), or denial-of-service via the public Open-Meteo free tier.

## Acknowledgements

Security researchers who responsibly disclose issues will be credited in the CHANGELOG release notes.
