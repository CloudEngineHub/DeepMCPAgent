# Security Policy

## Supported Versions

We actively support the following versions of Promptise with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report security vulnerabilities privately via **[GitHub Security Advisories](https://github.com/promptise-com/foundry/security/advisories/new)**.

You should receive a response within 48 hours. If for some reason you do not, please follow up on the same advisory to ensure we received your original report.

Please include the following information (as much as you can provide) to help us better understand the nature and scope of the possible issue:

* Type of issue (e.g., buffer overflow, SQL injection, cross-site scripting, etc.)
* Full paths of source file(s) related to the manifestation of the issue
* The location of the affected source code (tag/branch/commit or direct URL)
* Any special configuration required to reproduce the issue
* Step-by-step instructions to reproduce the issue
* Proof-of-concept or exploit code (if possible)
* Impact of the issue, including how an attacker might exploit the issue

This information will help us triage your report more quickly.

## Security Best Practices

When using Promptise in production:

### API Keys
- Never commit API keys to version control
- Use environment variables or secret management systems
- Rotate API keys regularly
- Use separate keys for development and production

### Agent Communication
- Enable JWT authentication for agent-to-agent communication
- Use TLS/HTTPS for all network communication
- Implement RBAC for agent permissions
- Enable audit logging for security events

### Sandbox Execution
- Always run untrusted code in sandboxed environments
- Use Docker or E2B sandbox modes
- Limit sandbox resource access
- Monitor sandbox activity

### Dependencies
- Keep dependencies up to date
- Use `pip install "promptise[all]"` to get the full security stack (ML guardrails, encrypted Redis cache)
- Run `pip audit` regularly
- Review dependency vulnerabilities

### Production Deployment
- Use non-root users in Docker containers
- Enable health checks and monitoring
- Implement rate limiting
- Use secrets management (HashiCorp Vault, AWS Secrets Manager, etc.)
- Enable security headers
- Implement proper error handling (don't leak sensitive info)

## Security Updates

Security updates will be released as:
- Patch releases for supported versions
- Mentioned in `CHANGELOG.md`
- Announced on GitHub Security Advisories

## Disclosure Policy

When we receive a security bug report, we will:

1. Confirm the problem and determine affected versions
2. Audit code to find any similar problems
3. Prepare fixes for all supported releases
4. Release new versions as soon as possible
5. Publish a security advisory on GitHub

## Contact

For any security concerns or questions:
- GitHub Security Advisories: https://github.com/promptise-com/foundry/security/advisories

## Attribution

We appreciate security researchers who report vulnerabilities to us. With your permission, we will:
- Acknowledge your contribution in the security advisory
- Credit you in the `CHANGELOG.md`
- Feature you in our security hall of fame (if desired)

Thank you for helping keep Promptise and our users safe!
