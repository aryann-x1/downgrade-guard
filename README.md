# downgrade-guard

A pre-deploy check that stops you from **downgrading a stateful service** (database, log store,
auth server) by deploying an older container image onto data that a newer version already
migrated.

It is one Python file that uses only the standard library and never changes anything. It reads
the versions running in your containers (Docker, Podman or Kubernetes), compares them with the
versions you're about to deploy, and exits non-zero if a deploy would downgrade something.

```
COMPONENT                    RUNNING  PIN            RESULT
OpenObserve                  1.0.3    0.92.2         STOP  newer than pin; deploying would downgrade it
Postgres                     17.11    17             OK
TimescaleDB extension (app)  2.28.2   2.17.0-2.30.1  OK
Redis                        8.4.0    8.4.0          info
```

## The problem

Many teams run stateful services from floating tags such as `:latest` or `:alpine`, so each
machine runs whatever version was current when it last pulled. Many stateful services migrate
their on-disk data to the version they run, and **they can't go back**.

Suppose you start pinning image versions, which is the right move. The first deploy of a pinned
build can still *downgrade* a machine that drifted ahead, and then the service won't start:

**OpenObserve** refuses to start on data that a newer version migrated:

```
DB_SCHEMA_VERSION mismatch : expected 64, found 77 … migration file is missing
```

**TimescaleDB:** each database records its extension version. An image can only load extension
versions whose libraries it ships (for example 2.17.0–2.30.1), and the extension can't be
downgraded:

```
could not access file "$libdir/timescaledb-2.29.2": No such file or directory
```

**Keycloak** migrates its database schema on startup. An older Keycloak can't run on a schema
that a newer one has migrated.

`downgrade-guard` runs before the deploy and blocks it while you can still choose a better pin.

## Quick start

Requirements: Python 3.11+, plus `docker` or `podman` on the machine where the containers run, or
`kubectl` with access to the cluster.

```sh
# 1. Download the single file
curl -fsSLO https://raw.githubusercontent.com/aryann-x1/downgrade-guard/main/downgrade_guard.py

# 2. Copy an example config and edit the container names and pins
curl -fsSL -o downgrade-guard.toml \
  https://raw.githubusercontent.com/aryann-x1/downgrade-guard/main/examples/postgres.toml

# 3. Run it
python3 downgrade_guard.py check            # reads ./downgrade-guard.toml
python3 downgrade_guard.py check -c other.toml
python3 downgrade_guard.py check --json     # machine-readable output
```

Examples: [`timescaledb.toml`](examples/timescaledb.toml), [`openobserve.toml`](examples/openobserve.toml),
[`keycloak.toml`](examples/keycloak.toml), [`postgres.toml`](examples/postgres.toml) and
[`kubernetes.toml`](examples/kubernetes.toml).

## Config reference

```toml
runtime = "podman"          # "docker" (default), "podman" or "kubectl"

[[check]]
name        = "OpenObserve"
container   = "openobserve"
version_cmd = ["/openobserve", "--version"]
pinned      = "0.92.2"
rule        = "not_newer"

[[check]]
name        = "Postgres"
container   = "db"
version_cmd = ["psql", "-U", "postgres", "-tAc", "show server_version"]
pinned      = "17"
rule        = "same_major"

[[check]]
name        = "TimescaleDB extension ({item})"
container   = "db"
for_each    = ["psql", "-U", "postgres", "-tAc", "select datname from pg_database where not datistemplate"]
version_cmd = ["psql", "-U", "postgres", "-d", "{item}", "-tAc", "select extversion from pg_extension where extname='timescaledb'"]
rule        = "within"
min         = "2.17.0"
max         = "2.30.1"

[[check]]
name          = "Redis"
container     = "redis"
version_cmd   = ["redis-server", "--version"]
version_regex = 'v=(\d+\.\d+\.\d+)'
pinned        = "8.4.0"
rule          = "info"
```

### Fields

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Label shown in the table. May contain `{item}` when `for_each` is used. |
| `container` | yes | Container name or ID. It must be running. With `kubectl`: a pod name or `<kind>/<name>`, see [Kubernetes](#kubernetes). |
| `pod_container` | no | `kubectl` only: the container inside a multi-container pod (`kubectl exec -c`). |
| `version_cmd` | yes | A command given as a list (no shell), run as `<runtime> exec <container> <cmd...>`. Its stdout and stderr are searched together, since some tools print their version to stderr. |
| `version_regex` | no | The first capture group is the version. Default: the first match of `\d+(\.\d+)+`, ignoring a leading `v`. |
| `for_each` | no | A command whose output lines (trimmed, non-empty) are items. The check runs once per item, with `{item}` replaced in `name` and `version_cmd`. Items whose version command prints nothing are skipped silently, for example a database without the extension. |
| `rule` | yes | See below. |
| `pinned` | for `not_newer`, `same_major`, `same_minor` (or use `pinned_from`) | The version you're about to deploy. It must be a quoted string (`"17"`, not `17`). |
| `pinned_from` | no | Read the pin from a Dockerfile, compose file or Kubernetes manifest instead. See [below](#pins-from-your-dockerfile-or-compose-file). |
| `min`, `max` | for `within` | The inclusive range of versions that the new image can run. |

Typos are caught: unknown fields, unknown rules and missing `pinned`/`min`/`max` are all
reported before anything runs.

### Pins from your Dockerfile or compose file

Writing the pin in two places, your deploy files and this config, means one day they'll
disagree. `pinned_from` reads it from the file you actually deploy:

```toml
[[check]]
name        = "Redis"
container   = "redis"
version_cmd = ["redis-server", "--version"]
rule        = "not_newer"
pinned_from = { file = "compose.yaml", image = "redis" }

[[check]]
name        = "Postgres"
container   = "db"
version_cmd = ["psql", "-U", "postgres", "-tAc", "show server_version"]
rule        = "same_major"
# timescale/timescaledb:2.30.1-pg17 -> "17"
pinned_from = { file = "Dockerfile", image = "timescale/timescaledb", tag_regex = 'pg(\d+)' }
```

| Key | Meaning |
|---|---|
| `file` | Path relative to the config file. |
| `image` | The image to look for. Docker Hub names match however they're written: `redis`, `library/redis` and `docker.io/library/redis` are the same image. Other registries must match exactly (`quay.io/keycloak/keycloak`). |
| `tag_regex` | Optional. The first capture group of this regex, applied to the tag, is the pin. Default: the whole tag (`8.4.0-alpine` compares as `8.4.0`). |

How files are read:

- **Dockerfiles** (any file not ending in `.yaml`/`.yml`): `FROM` lines. A `FROM` that names an
  earlier build stage (`FROM base`) is skipped. Variables come from `ARG VAR=default` lines
  *before the first `FROM`*: those are the only ones Docker lets a `FROM` use. The environment
  isn't used, because `docker build` doesn't use it either.
- **Compose files and Kubernetes manifests** (`.yaml`/`.yml`): `image:` lines. Variables come from
  the environment, then from a `.env` file next to the compose file, with compose's quoting and
  `# comment` rules. YAML aliases (`image: *redis-image`) are followed to their anchor
  (`x-redis-image: &redis-image redis:8.4.0`); an alias that can't be followed is an error.
- **Compose override files:** if `file` is a default compose name (`compose.yaml`,
  `compose.yml`, `docker-compose.yaml` or `docker-compose.yml`), any
  `compose.override.yaml`-style file next to it is read too, because `docker compose` merges it
  automatically.
- **Variable syntax:** the compose forms all work: `$VAR`, `${VAR}`, `${VAR:-default}`,
  `${VAR-default}`, `${VAR:?error}`, `${VAR:+alt}`, `${VAR+alt}` and `$$` for a literal `$`.

It fails closed. The config is rejected (exit 2) if any of these is true:

- the image isn't in the file;
- the image appears with different tags, including between a compose file and its override;
- the image has no tag, or a tag that isn't a version (`latest`, `alpine`);
- a variable can't be resolved on a line that is, or *might be*, this image (`image: ${IMAGE}`).

A variable that can't be resolved in *another* image's tag is ignored. `pinned_from` works with
every rule except `within`.

Run the check in the same directory and with the same environment variables as the deploy, so
variables resolve the same way. Things compose takes from its command line aren't seen:
`-f a.yaml -f b.yaml`, `COMPOSE_FILE`, `--env-file`. With those, point `file` at the file that
sets the image, and export the variables before running the check.

### Kubernetes

Set `runtime = "kubectl"` to check pods through `kubectl exec`
([example](examples/kubernetes.toml)):

```toml
runtime   = "kubectl"
namespace = "data"          # optional; default: the context's namespace
context   = "production"    # optional; default: the current kubectl context

[[check]]
name          = "Postgres"
container     = "statefulset/db"
pod_container = "postgres"
version_cmd   = ["psql", "-U", "postgres", "-tAc", "show server_version"]
pinned        = "17"
rule          = "same_major"
```

Needs kubectl 1.21 or newer. `container` is anything `kubectl exec` accepts: a pod name (`db-0`) or `<kind>/<name>`
(`statefulset/db`, `deployment/keycloak`). For a kind/name, kubectl picks one of its pods, which
is fine because they all run the same image. If replicas might be on different versions halfway
through a rollout, list the pods individually.

### Rules

| Rule | Passes when | Use for |
|---|---|---|
| `not_newer` | running ≤ pinned | Services that migrate data forward on startup (OpenObserve, Keycloak). |
| `within` | min ≤ running ≤ max | Versions the new image can load from a range (TimescaleDB extension). |
| `same_major` | major numbers equal | Postgres data directories. |
| `same_minor` | major.minor equal | Services whose on-disk format changes with minor releases. |
| `info` | always | Just report the version. |

### How versions are compared

The tool strips a leading `v`, takes the numeric dotted part before any suffix
(`8.4.0-alpine` → `8.4.0`), and compares the parts as numbers, so `2.9.0 < 2.17.0`. Missing
parts count as zero, so `17` equals `17.0`, and under `not_newer` a running `17.11` is newer
than a pin of `17`. Use `same_major` when that's what you mean. Pre-release suffixes are
ignored: `2.17.0rc1` compares as `2.17.0`.

### Results and exit codes

| Result | Meaning |
|---|---|
| `OK` | Safe to deploy. |
| `STOP` | Deploying would downgrade the service, or leave it on a version the new image can't run. |
| `CHECK` | Couldn't be checked: container not running or not found, command failed, version not found. The reason is shown. |
| `info` | Reported only. |

| Exit code | Meaning |
|---|---|
| `0` | Everything is OK or info. |
| `1` | At least one STOP. This wins even if something else couldn't be checked. |
| `2` | Something couldn't be checked, or the config is invalid. |

### JSON output

`check --json` prints the same results for other tools. The exit codes don't change.

```json
{
  "tool_version": "0.2.0",
  "exit_code": 1,
  "results": [
    {
      "component": "OpenObserve",
      "running": "1.0.3",
      "pin": "0.92.2",
      "result": "STOP",
      "reason": "newer than pin; deploying would downgrade it"
    }
  ]
}
```

`running`, `pin` and `reason` are `null` when there's nothing to show. If the config is invalid,
`results` is empty and `error` holds the message.

### What it runs

It runs only `<runtime> container inspect` or `kubectl get` (to see whether a container is
running) and your `version_cmd` / `for_each` commands through `<runtime> exec` or `kubectl exec`.
It reads the config and any `pinned_from` files (plus a `.env` next to a compose file). It never
starts, stops, pulls or writes anything. Your version commands should be read-only too, such as `--version` flags and
`select` queries. Each command times out after 30 seconds.

## Using it as a deploy gate

Run it on the target machine right before the deploy step:

```sh
python3 downgrade_guard.py check || exit 1
docker compose pull && docker compose up -d
```

Exit code 2 (couldn't check) also blocks the deploy. That is deliberate: a container that isn't
running isn't proof that the deploy is safe. On a fresh machine with no containers yet, skip the
check or use a config that lists only what already exists.

### In CI (GitHub Actions over SSH)

```yaml
deploy:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v7
    - name: Upload the new compose file and the guard
      # Copying files changes nothing that's running; only `docker compose up` does.
      run: scp compose.yaml downgrade-guard.toml downgrade_guard.py deploy@$HOST:/srv/app/
    - name: Check for downgrades
      # Same directory, .env and override files as the deploy below, so pinned_from
      # reads exactly what docker compose will deploy.
      run: ssh deploy@$HOST 'cd /srv/app && python3 downgrade_guard.py check'
    - name: Deploy
      run: ssh deploy@$HOST 'cd /srv/app && docker compose pull && docker compose up -d'
```

If the check step fails, the deploy step doesn't run. The table in the job log shows which
component would have been downgraded. Don't run the check from a shared directory such as
`/tmp`: `pinned_from` would read whatever `.env` it finds there.

## Per-service notes

### TimescaleDB: why `within`

TimescaleDB is a Postgres extension. Each database stores the extension version it was last
updated to (`select extversion from pg_extension`), and on connect Postgres loads
`$libdir/timescaledb-<that version>.so`. An image ships a *range* of these libraries, so the
new image works as long as every database's extension version is in that range:

- **Below `min`:** the new image no longer ships that library. Upgrade the extension
  (`ALTER EXTENSION timescaledb UPDATE`) on the old image first.
- **Above `max`:** the database was already updated by a newer image, and the extension can't be
  downgraded. Pin a newer image.

Check the range an image ships with:

```sh
docker run --rm timescale/timescaledb:2.30.1-pg17 \
  sh -c 'ls /usr/local/lib/postgresql | grep -E "^timescaledb-[0-9]"'
```

The first and last versions listed are your `min` and `max`.

Use `for_each` to check every database. Different databases can be on different extension
versions.

### OpenObserve: why `not_newer`

OpenObserve migrates its metadata database on startup and records the schema version. An older
binary doesn't have the newer migration files, so it refuses to start (`DB_SCHEMA_VERSION
mismatch … migration file is missing`). Any running version newer than the pin is a STOP.

### Keycloak: why `not_newer`

Keycloak runs its database migrations (Liquibase) on startup. Once a newer Keycloak has migrated
the schema, an older one can't safely run on it. Get the version from
`/opt/keycloak/bin/kc.sh --version`, and use `version_regex = 'Keycloak (\d+\.\d+\.\d+)'` because
the output also contains JVM and OS versions. Also check Keycloak's database container (see
[`keycloak.toml`](examples/keycloak.toml)).

### Postgres: why `same_major`

A Postgres data directory works only with the major version that created it. Moving between
majors needs `pg_upgrade` or dump/restore, in either direction. Minor releases within a major
are interchangeable.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests mock the subprocess layer, so they need no containers. CI runs them on Python 3.11,
3.12 and 3.13.

## Roadmap

v0.2 added `pinned_from`, `--json` and Kubernetes (see the [changelog](CHANGELOG.md)). Ideas
for later:

- `pinned_from` for Helm values files, where the repository and tag are separate keys.
- `pinned_from` for compose files that set the image through a variable for the whole name
  (`image: ${IMAGE}`).

## License

[MIT](LICENSE) © Aryan Pradeep Arora
