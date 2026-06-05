# Security Policy

## Supported Versions

This project is currently private and in active testing. Security fixes apply to the latest `main` branch.

## Reporting a Vulnerability

Please report security issues privately to the repository owner rather than opening a public issue.

For this private testing phase, contact [@DUBSOpenHub](https://github.com/DUBSOpenHub) directly with:

- a short description of the issue,
- steps to reproduce,
- affected files or commands,
- any suggested mitigation.

Do not include secrets, tokens, private run bundles, or confidential repository data in reports.

## Security Notes

- Generated run bundles may contain project metadata and judge outputs; treat them as internal artifacts.
- Winner cards require human approval before external publishing.
- Replay and presentation commands should use stored artifacts only; avoid adding live model calls to replay paths.

