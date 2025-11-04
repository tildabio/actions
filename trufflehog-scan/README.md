# TruffleHog Secret Scanner

Scans filesystem for verified secrets using TruffleHog.

## Features

- ✅ Scans for 700+ credential types
- ✅ Verifies secrets are active/valid
- ✅ Human-readable formatted output
- ✅ Configurable fail behavior
- ✅ Uploads results as artifacts

## Usage

```yaml
- name: Scan for secrets
  uses: tildabio/actions/trufflehog-scan@main
  with:
    scan-path: '/scan'
    only-verified: true
    fail-on-findings: true
```

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `scan-path` | Path to scan (relative to repository root) | No | `.` |
| `only-verified` | Only report verified secrets | No | `true` |
| `fail-on-findings` | Fail the workflow if secrets are found | No | `true` |
| `extra-args` | Additional arguments to pass to TruffleHog | No | `''` |

## Outputs

| Output | Description |
|--------|-------------|
| `secrets-found` | Number of secrets found |
| `scan-status` | Status: `clean` or `secrets-found` |

## Example Output

```
🚨 TruffleHog found 2 VERIFIED secret(s)!

================================================================================

#1 Github Secret ✅ VERIFIED
   Location: common/constants/constants.go:140
   Type: GitHub Personal Access Token
   Details:
      • email: user@company.com
      • username: username
      • expiry: 2025-12-21
      • 🔄 Rotation guide: https://howtorotate.com/docs/tutorials/github/
--------------------------------------------------------------------------------

#2 OpenAI Secret ✅ VERIFIED
   Location: config/api-key.txt:1
   Type: OpenAI API Key
   Redacted: sk-...xxxxx
--------------------------------------------------------------------------------

⚠️  CRITICAL: 2 verified secret(s) detected!
Action required: Rotate these credentials immediately.
```
