# Changelog

## 0.2.0

- `pinned_from`: read a check's pin from a Dockerfile (`FROM`, with global `ARG` defaults), a
  compose file (`image:`, with the environment, `.env` and override files) or a Kubernetes
  manifest. `tag_regex` pulls the version out of tags such as `2.30.1-pg17`. It fails closed on
  anything ambiguous, such as an unresolved variable that might be the image.
- `check --json`: machine-readable output with the same exit codes.
- Kubernetes: `runtime = "kubectl"` checks pods through `kubectl exec` (kubectl 1.21+), with
  optional `namespace`, `context` and per-check `pod_container`.
- Fixed: a `version_regex` whose capture group is optional no longer crashes the tool.

## 0.1.0

- First release: `check` command; `not_newer`, `within`, `same_major`, `same_minor` and `info`
  rules; `for_each`; Docker and Podman.
