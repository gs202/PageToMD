# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

Please report security issues **privately** using GitHub's
["Report a vulnerability"](https://github.com/gs202/pagetomd/security/advisories/new)
feature (Security tab). Do not open a public issue for security reports.

We aim to acknowledge reports within 72 hours and to provide a remediation
timeline after triage.

## Threat Model & Known Limitations

`pagetomd` is a **public-URL-only** CLI intended for a single trusted user. Note:

- **SSRF default-deny:** the tool refuses to fetch loopback, link-local,
  private, multicast, reserved, and cloud-metadata addresses by default, and
  re-validates HTTP redirects and meta-refresh hops on the `httpx` path.
- **DNS Rebinding TOCTOU Limitation:** While `guard_url(url)` resolves DNS to ensure the IP is safe, Python HTTP transports (`httpx` and `playwright`) perform an independent DNS resolution during the actual fetch. An attacker with a custom DNS server and a low TTL could theoretically return a public IP during the check and a private IP during the fetch (Time-of-Check to Time-of-Use). Given `pagetomd` is a CLI tool rather than a public web service, this residual risk is acceptable.
- **Playwright path caveat:** with `--fetcher playwright`/`auto`, only the
  initial URL is SSRF-guarded; in-browser redirects are not yet re-validated
  per hop. Prefer the default `httpx` fetcher for untrusted input.
- **`--follow-symlinks`** is off by default; enabling it allows writes through
  a symlinked destination.
- **`--wide-tables html`** emits passthrough HTML that is scrubbed of event
  handlers and `javascript:`/`vbscript:`/`data:text/html` URLs, but treat any
  rendered output as having the same trust level as its source page.
