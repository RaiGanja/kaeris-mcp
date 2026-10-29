# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Email **support@kaeris.dev** with
"Security" in the subject line, or use GitHub's private vulnerability reporting
(the repository's **Security** tab, **Report a vulnerability**).

Please include:

- the affected package and version (`kaeris --version`, `pip show kaeris-mcp`, or the action tag);
- steps to reproduce, and what an attacker gains;
- whether the issue is already public anywhere.

We reply within 3 working days, keep you informed until the fix ships, and credit you in the
release notes unless you ask us not to.

## Supported versions

Fixes go into the latest release of each package. Upgrade with `pip install -U kaeris`,
`pip install -U kaeris-mcp`, or move the action to the latest `v2` tag.

## Scope

In scope: the code in the `kaeris-io` repositories and the API at `kaeris.dev` that they call.
How the service handles your data is described at <https://kaeris.dev/security.html>.
