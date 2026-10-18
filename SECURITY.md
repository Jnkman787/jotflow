# Security policy

JotFlow writes to GitHub repositories based on content posted in chat. Treat anything that could let untrusted channel content cause an unintended write, bypass the approval gate, or expose message content as a security issue.

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use GitHub's private vulnerability reporting on this repository: go to the **Security** tab and choose **Report a vulnerability**. You will get an acknowledgement within a few days and a status update once the report has been assessed.

Please include steps to reproduce, the affected component (orchestrator, MCP server, agent adapter, execution runs), and what an attacker gains.

## Scope

In scope:

- Bypassing the per-channel approval gate
- Writes outside the configured repo, label, or project allowlist
- Prompt injection paths that reach a write action
- Leakage or unintended storage of Slack message content
- Secret exposure in logs, configuration, or CI

Out of scope:

- Vulnerabilities in the agent a team chooses to connect
- Issues in Slack or GitHub themselves

## Supported versions

JotFlow is pre-release. Only the `main` branch is supported.
