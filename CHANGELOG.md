# Changelog

## 0.2.0

- `pinned_from`: read a check's pin from a Dockerfile (`FROM`, with `ARG` defaults), a compose
  file (`image:`, with the environment and `.env`) or a Kubernetes manifest. `tag_regex` pulls
  the version out of tags such as `2.30.1-pg17`.
- `check --json`: machine-readable output with the same exit codes.
- Kubernetes: `runtime = "kubectl"` checks pods through `kubectl exec`, with optional
  `namespace`, `context` and per-check `pod_container`.

## 0.1.0

- First release: `check` command; `not_newer`, `within`, `same_major`, `same_minor` and `info`
  rules; `for_each`; Docker and Podman.
