# Security Policy

## Supported version

Security fixes are made on the latest `main` branch. Older commits and local
runtime releases should be updated after a fix is published.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** flow in the repository Security tab.
Do not open a public issue for a suspected vulnerability.

Include:

- the affected commit or release
- a minimal reproduction using synthetic data
- the security impact
- a suggested mitigation, if known

Do not include OAuth client secrets, refresh or access tokens, Apple or Google
account identifiers, reminder or task content, local absolute paths, raw
diagnostic files, or exported user data. Redact those values before submitting
a report.

The maintainer will acknowledge a report when it is reviewed, validate it
privately, and coordinate a fix before public disclosure when appropriate.
