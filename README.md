# downgrade-guard

A pre-deploy check that stops you from **downgrading a stateful service** (database, log store,
auth server) by deploying an older container image onto data that a newer version already
migrated.

It is one Python file that uses only the standard library and never changes anything. It reads
the versions running in your containers, compares them with the versions you're about to deploy,
and exits non-zero if a deploy would downgrade something.

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

Requirements: Python 3.11+, plus `docker` or `podman` on the machine where the containers run.

```sh
# 1. Download the single file
curl -fsSLO https://raw.githubusercontent.com/aryann-x1/downgrade-guard/main/downgrade_guard.py

# 2. Copy an example config and edit the container names and pins
curl -fsSL -o downgrade-guard.toml \
  https://raw.githubusercontent.com/aryann-x1/downgrade-guard/main/examples/postgres.toml

# 3. Run it
python3 downgrade_guard.py check            # reads ./downgrade-guard.toml
python3 downgrade_guard.py check -c other.toml
```

Examples: [`timescaledb.toml`](examples/timescaledb.toml), [`openobserve.toml`](examples/openobserve.toml),
[`keycloak.toml`](examples/keycloak.toml) and [`postgres.toml`](examples/postgres.toml).

## Config reference

```toml
runtime = "podman"          # "docker" (default) or "podman"

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
| `container` | yes | Container name or ID. It must be running. |
| `version_cmd` | yes | A command given as a list (no shell), run as `<runtime> exec <container> <cmd...>`. Its stdout and stderr are searched together, since some tools print their version to stderr. |
| `version_regex` | no | The first capture group is the version. Default: the first match of `\d+(\.\d+)+`, ignoring a leading `v`. |
| `for_each` | no | A command whose output lines (trimmed, non-empty) are items. The check runs once per item, with `{item}` replaced in `name` and `version_cmd`. Items whose version command prints nothing are skipped silently, for example a database without the extension. |
| `rule` | yes | See below. |
| `pinned` | for `not_newer`, `same_major`, `same_minor` | The version you're about to deploy. It must be a quoted string (`"17"`, not `17`). |
| `min`, `max` | for `within` | The inclusive range of versions that the new image can run. |

Typos are caught: unknown fields, unknown rules and missing `pinned`/`min`/`max` are all
reported before anything runs.

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

### What it runs

It runs only `<runtime> container inspect` (to see whether a container is running) and your
`version_cmd` / `for_each` commands through `<runtime> exec`. It never starts, stops, pulls or
writes anything. Your version commands should be read-only too, such as `--version` flags and
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
    - uses: actions/checkout@v4
    - name: Check for downgrades on the server
      run: |
        scp downgrade_guard.py downgrade-guard.toml deploy@$HOST:/tmp/
        ssh deploy@$HOST 'cd /tmp && python3 downgrade_guard.py check'
    - name: Deploy
      run: ssh deploy@$HOST 'cd /srv/app && docker compose pull && docker compose up -d'
```

If the check step fails, the deploy step doesn't run. The table in the job log shows which
component would have been downgraded.

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
  sh -c 'ls /usr/local/lib/postgresql/timescaledb-*.so'
```

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

Not in v0.1:

- **`pinned_from`:** read pins from Dockerfiles or compose files instead of repeating them in the
  config.
- **`--json` output** for other tools to consume.
- **Kubernetes:** check pods through `kubectl exec`.

## License

[MIT](LICENSE) © Aryan Pradeep Arora
