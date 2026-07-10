# Security Policy

## Supported Versions

Security fixes apply to the latest `main` branch.

## Reporting a Vulnerability

Please report security issues privately to the repository owner rather than opening a public issue.

Contact [@DUBSOpenHub](https://github.com/DUBSOpenHub) directly with:

- a short description of the issue,
- steps to reproduce,
- affected files or commands,
- any suggested mitigation.

Do not include secrets, tokens, private run bundles, or confidential repository data in reports.

## Repository Safeguards

- Dependabot alerts and Dependabot security updates are enabled.
- The repository contains a scheduled CodeQL workflow with least-privilege
 `security-events: write` permissions.
- Generated run bundles are ignored by Git and require human approval before
 external publication.
- Run IDs are validated before they are resolved below the configured runs
 directory.
- Replay archives reject path traversal, links, and device entries before
 extraction.
- Exported bundles are write-once and cannot be re-sealed.

## GitHub Advanced Security

Secret scanning, push protection, and private-repository code-scanning uploads
require GitHub Advanced Security for this organization. The platform currently
reports that Advanced Security has not been purchased, so those controls cannot
be activated without an organization license. Do not make this repository
public as a workaround: generated bundles and judging artifacts are internal.

Once the organization enables GitHub Advanced Security, enable these repository
controls immediately:

1. Secret scanning
2. Secret-scanning push protection
3. Code scanning, then set the CodeQL workflow's upload mode to `always`

## Application Notes

- Generated run bundles may contain project metadata and judge outputs; treat
 them as internal artifacts.
- Winner cards require human approval before external publishing.
- Replay and presentation commands use stored artifacts only; do not add live
 model calls to replay paths.
