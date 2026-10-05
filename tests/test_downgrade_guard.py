import contextlib
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import downgrade_guard as dg  # noqa: E402

SPEC_CONFIG = """
runtime = "podman"

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
version_regex = 'v=(\\d+\\.\\d+\\.\\d+)'
pinned        = "8.4.0"
rule          = "info"
"""

LIST_DBS = ("psql", "-U", "postgres", "-tAc",
            "select datname from pg_database where not datistemplate")
EXT_SQL = "select extversion from pg_extension where extname='timescaledb'"
SPEC_OUTPUTS = {
    ("openobserve", "/openobserve", "--version"): (0, "openobserve v1.0.3\n"),
    ("db", "psql", "-U", "postgres", "-tAc", "show server_version"):
        (0, "17.11 (Debian 17.11-1.pgdg120+1)\n"),
    ("db", *LIST_DBS): (0, "postgres\napp\n"),
    ("db", "psql", "-U", "postgres", "-d", "postgres", "-tAc", EXT_SQL): (0, "\n"),
    ("db", "psql", "-U", "postgres", "-d", "app", "-tAc", EXT_SQL): (0, "2.28.2\n"),
    ("redis", "redis-server", "--version"):
        (0, "Redis server v=8.4.0 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64\n"),
}


def fake_run(execs, containers=None):
    """A stand-in for dg.run.

    execs:      {(container, *cmd): (exit code, output)}; unknown commands fail.
    containers: {name: "true" | "false" | None}; None = no such container,
                containers not listed are running.
    """
    containers = containers or {}
    calls = []

    def run(args, timeout=None):
        calls.append(list(args))
        verb = args[1]
        if verb == "container":
            assert args[2] == "inspect", args
            name = args[-1]
            state = containers.get(name, "true")
            if state is None:
                return 125, f"Error: no such container {name}\n"
            return 0, state + "\n"
        assert verb == "exec", f"unexpected command {args}"
        return execs.get(tuple(args[2:]), (127, "executable file not found in $PATH\n"))

    run.calls = calls
    return run


def spec_config():
    return tomllib.loads(SPEC_CONFIG)


def check(**fields):
    base = {"name": "Thing", "container": "c", "version_cmd": ["thing", "--version"]}
    return {**base, **fields}


class ParseVersionTest(unittest.TestCase):
    def test_parsing(self):
        cases = {
            "1.2.3": (1, 2, 3),
            "17": (17,),
            "17.11": (17, 11),
            "v1.0.3": (1, 0, 3),
            "V2.0": (2, 0),
            "8.4.0-alpine": (8, 4, 0),
            "v8.4.0-alpine3.21": (8, 4, 0),
            "26.4.0.Final": (26, 4, 0),
            "2.17.0rc1": (2, 17, 0),
            "17.11 (Debian 17.11-1.pgdg120+1)": (17, 11),
            "  2.28.2\n": (2, 28, 2),
            "007.010": (7, 10),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dg.parse_version(text), expected)

    def test_rejects_non_versions(self):
        for text in ["", "latest", "alpine", "v", "vv1.2", "-1.2", ".1"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                dg.parse_version(text)


class CompareVersionsTest(unittest.TestCase):
    def test_ordering(self):
        cases = [
            ("2.9.0", "2.17.0", -1),    # numeric, not string, comparison
            ("2.17.0", "2.9.0", 1),
            ("1.0.3", "0.92.2", 1),
            ("0.92.2", "1.0.3", -1),
            ("0.9.10", "0.9.9", 1),
            ("10.0", "9.99.99", 1),
            ("1.2.3", "1.2.3", 0),
            ("v1.2.3", "1.2.3", 0),
            ("v1.2.3", "v1.2.4", -1),
            ("8.4.0-alpine", "8.4.0", 0),
            ("8.4.1-alpine", "8.4.0-bookworm", 1),
            ("17", "17.11", -1),       # 17 is padded to 17.0
            ("17.11", "17", 1),
            ("17", "17.0", 0),
            ("17", "17.0.0", 0),
            ("2", "1.99.99", 1),
            ("2.30.1", "2.30.1.1", -1),
            ("2.30.1.0", "2.30.1", 0),
            ("0.0.1", "0.0.0", 1),
        ]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(dg.compare_versions(a, b), expected)

    def test_antisymmetric(self):
        versions = ["0.92.2", "1.0.3", "2.9.0", "2.17.0", "17", "17.11", "v8.4.0-alpine"]
        for a in versions:
            for b in versions:
                with self.subTest(a=a, b=b):
                    self.assertEqual(dg.compare_versions(a, b), -dg.compare_versions(b, a))


class ExtractVersionTest(unittest.TestCase):
    def test_default_regex(self):
        cases = {
            "openobserve v1.0.3\n": "1.0.3",
            "17.11 (Debian 17.11-1.pgdg120+1)\n": "17.11",
            "2.28.2\n": "2.28.2",
            "Keycloak 26.4.0\nJVM: 21.0.8 (Red Hat, Inc.)\n": "26.4.0",
            "Redis server v=8.4.0 sha=00000000:0 malloc=jemalloc-5.3.0": "8.4.0",
            "warning: something\nmyapp version 3.1": "3.1",
        }
        for output, expected in cases.items():
            with self.subTest(output=output):
                self.assertEqual(dg.extract_version(output), expected)

    def test_custom_regex_uses_first_group(self):
        output = "Keycloak 26.4.0\nJVM: 21.0.8\nOS: Linux 6.1.0"
        self.assertEqual(dg.extract_version(output, r"JVM: (\d+\.\d+\.\d+)"), "21.0.8")

    def test_not_found(self):
        self.assertIsNone(dg.extract_version("no version here"))
        self.assertIsNone(dg.extract_version("17"))  # default needs at least one dot
        self.assertIsNone(dg.extract_version("abc", r"v=(\d+)"))


class EvaluateTest(unittest.TestCase):
    def result(self, running, **fields):
        return dg.evaluate(check(**fields), running)[0]

    def test_not_newer(self):
        self.assertEqual(self.result("0.92.2", rule="not_newer", pinned="0.92.2"), "OK")
        self.assertEqual(self.result("0.9.0", rule="not_newer", pinned="0.92.2"), "OK")
        self.assertEqual(self.result("1.0.3", rule="not_newer", pinned="0.92.2"), "STOP")
        self.assertEqual(self.result("17.11", rule="not_newer", pinned="17"), "STOP")
        self.assertEqual(self.result("17.0", rule="not_newer", pinned="17"), "OK")

    def test_not_newer_reasons(self):
        self.assertEqual(dg.evaluate(check(rule="not_newer", pinned="0.92.2"), "1.0.3"),
                         ("STOP", "newer than pin; deploying would downgrade it"))
        self.assertEqual(dg.evaluate(check(rule="not_newer", pinned="8.4.0"), "8.2.0"),
                         ("OK", "older than pin; deploy will upgrade"))
        self.assertEqual(dg.evaluate(check(rule="not_newer", pinned="8.4.0"), "8.4.0"), ("OK", ""))

    def test_within(self):
        rng = {"rule": "within", "min": "2.17.0", "max": "2.30.1"}
        self.assertEqual(self.result("2.17.0", **rng), "OK")
        self.assertEqual(self.result("2.28.2", **rng), "OK")
        self.assertEqual(self.result("2.30.1", **rng), "OK")
        self.assertEqual(self.result("2.9.0", **rng), "STOP")
        self.assertEqual(self.result("2.16.9", **rng), "STOP")
        self.assertEqual(self.result("2.30.2", **rng), "STOP")
        self.assertEqual(self.result("3.0", **rng), "STOP")
        self.assertIn("older than min", dg.evaluate(check(**rng), "2.9.0")[1])
        self.assertIn("newer than max", dg.evaluate(check(**rng), "2.31.0")[1])

    def test_same_major(self):
        self.assertEqual(self.result("17.11", rule="same_major", pinned="17"), "OK")
        self.assertEqual(self.result("17.0", rule="same_major", pinned="17.11"), "OK")
        self.assertEqual(self.result("18.1", rule="same_major", pinned="17"), "STOP")
        self.assertEqual(self.result("16.9", rule="same_major", pinned="17"), "STOP")
        self.assertIn("downgrade", dg.evaluate(check(rule="same_major", pinned="17"), "18.0")[1])
        self.assertIn("upgrade", dg.evaluate(check(rule="same_major", pinned="17"), "16.4")[1])

    def test_same_minor(self):
        self.assertEqual(self.result("8.4.0", rule="same_minor", pinned="8.4"), "OK")
        self.assertEqual(self.result("8.4.7", rule="same_minor", pinned="8.4.0"), "OK")
        self.assertEqual(self.result("8", rule="same_minor", pinned="8.0.3"), "OK")
        self.assertEqual(self.result("8.5.0", rule="same_minor", pinned="8.4"), "STOP")
        self.assertEqual(self.result("8.2.0", rule="same_minor", pinned="8.4"), "STOP")
        self.assertEqual(self.result("9.4.0", rule="same_minor", pinned="8.4"), "STOP")

    def test_info_never_fails(self):
        self.assertEqual(dg.evaluate(check(rule="info", pinned="1.0"), "99.0"), ("info", ""))
        self.assertEqual(dg.evaluate(check(rule="info"), "1.0"), ("info", ""))

    def test_pin_label(self):
        self.assertEqual(dg.pin_label(check(rule="within", min="2.17.0", max="2.30.1")),
                         "2.17.0-2.30.1")
        self.assertEqual(dg.pin_label(check(rule="not_newer", pinned="1.2")), "1.2")
        self.assertEqual(dg.pin_label(check(rule="info")), "-")


class ValidateConfigTest(unittest.TestCase):
    def errors(self, **fields):
        return dg.validate_config({"check": [check(**fields)]})

    def assertError(self, errors, fragment):
        self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")

    def test_spec_config_is_valid(self):
        self.assertEqual(dg.validate_config(spec_config()), [])

    def test_examples_are_valid(self):
        examples = sorted((ROOT / "examples").glob("*.toml"))
        self.assertTrue(examples)
        for path in examples:
            with self.subTest(path=path.name):
                dg.load_config(path)

    def test_readme_examples_are_valid(self):
        readme = (ROOT / "README.md").read_text()
        blocks = re.findall(r"```toml\n(.*?)```", readme, re.DOTALL)
        self.assertGreaterEqual(len(blocks), 3)
        for block in blocks:
            with self.subTest(block=block[:40]):
                self.assertEqual(dg.validate_config(tomllib.loads(block)), [])

    def test_unknown_rule(self):
        self.assertError(self.errors(rule="newest"), "'rule' must be one of")

    def test_missing_rule(self):
        self.assertError(self.errors(), "'rule' must be one of")

    def test_missing_rule_fields(self):
        self.assertError(self.errors(rule="not_newer"), "rule 'not_newer' needs 'pinned'")
        self.assertError(self.errors(rule="same_major"), "needs 'pinned'")
        self.assertError(self.errors(rule="same_minor"), "needs 'pinned'")
        self.assertError(self.errors(rule="within", min="1.0"), "rule 'within' needs 'max'")
        self.assertError(self.errors(rule="within", max="1.0"), "rule 'within' needs 'min'")
        self.assertEqual(self.errors(rule="info"), [])

    def test_versions_must_be_quoted_strings(self):
        self.assertError(self.errors(rule="same_major", pinned=17), 'pinned = "17"')
        self.assertError(self.errors(rule="not_newer", pinned="latest"), "not a version")

    def test_min_greater_than_max(self):
        self.assertError(self.errors(rule="within", min="2.30.1", max="2.17.0"), "greater than")

    def test_required_fields(self):
        errors = dg.validate_config({"check": [{"rule": "info"}]})
        self.assertError(errors, "'name' must be")
        self.assertError(errors, "'container' must be")
        self.assertError(errors, "'version_cmd' must be")

    def test_version_cmd_must_be_list(self):
        self.assertError(self.errors(rule="info", version_cmd="redis-server --version"),
                         "'version_cmd' must be a non-empty list")
        self.assertError(self.errors(rule="info", version_cmd=[]), "'version_cmd'")
        self.assertError(self.errors(rule="info", for_each="ls"), "'for_each'")

    def test_unknown_field(self):
        self.assertError(self.errors(rule="not_newer", pinned="1", pin="1"),
                         "unknown field 'pin'")

    def test_item_without_for_each(self):
        self.assertError(self.errors(rule="info", name="X ({item})"), "{item}")

    def test_regex(self):
        self.assertError(self.errors(rule="info", version_regex="(unclosed"), "not a valid regex")
        self.assertError(self.errors(rule="info", version_regex=r"\d+"), "capture group")
        self.assertEqual(self.errors(rule="info", version_regex=r"v=(\d+)"), [])

    def test_top_level(self):
        self.assertError(dg.validate_config({}), "no checks defined")
        self.assertError(dg.validate_config({"check": []}), "no checks defined")
        self.assertError(dg.validate_config({"runtime": "lxc", "check": [check(rule="info")]}),
                         "runtime must be one of")
        self.assertError(dg.validate_config({"runtme": "podman", "check": [check(rule="info")]}),
                         "unknown top-level key 'runtme'")

    def test_errors_name_the_check(self):
        errors = dg.validate_config({"check": [check(rule="info"), check(name="Second", rule="x")]})
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("check #2 (Second): "))


class LoadConfigTest(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_missing_file(self):
        with self.assertRaisesRegex(dg.ConfigError, "not found"):
            dg.load_config("/nonexistent/downgrade-guard.toml")

    def test_invalid_toml(self):
        with self.assertRaisesRegex(dg.ConfigError, "invalid TOML"):
            dg.load_config(self.write("[[check]\nname = "))

    def test_reports_all_errors(self):
        path = self.write('[[check]]\nname = "A"\nrule = "nope"\n')
        with self.assertRaises(dg.ConfigError) as ctx:
            dg.load_config(path)
        message = str(ctx.exception)
        self.assertIn("'container'", message)
        self.assertIn("'version_cmd'", message)
        self.assertIn("'rule'", message)

    def test_runtime_defaults_to_docker(self):
        path = self.write('[[check]]\nname="A"\ncontainer="a"\nversion_cmd=["a"]\nrule="info"\n')
        self.assertEqual(dg.load_config(path)["runtime"], "docker")


class CheckAllTest(unittest.TestCase):
    def rows(self, config, execs, containers=None):
        runner = fake_run(execs, containers)
        with mock.patch.object(dg, "run", runner):
            return dg.check_all(config), runner.calls

    def test_spec_example(self):
        rows, _ = self.rows(spec_config(), SPEC_OUTPUTS)
        self.assertEqual(rows, [
            dg.Row("OpenObserve", "1.0.3", "0.92.2", "STOP",
                   "newer than pin; deploying would downgrade it"),
            dg.Row("Postgres", "17.11", "17", "OK"),
            dg.Row("TimescaleDB extension (app)", "2.28.2", "2.17.0-2.30.1", "OK"),
            dg.Row("Redis", "8.4.0", "8.4.0", "info"),
        ])
        self.assertEqual(dg.exit_code(rows), dg.EXIT_STOP)

    def test_table_output(self):
        rows, _ = self.rows(spec_config(), SPEC_OUTPUTS)
        self.assertEqual(dg.format_table(rows).splitlines(), [
            "COMPONENT                    RUNNING  PIN            RESULT",
            "OpenObserve                  1.0.3    0.92.2         STOP  newer than pin; deploying would downgrade it",
            "Postgres                     17.11    17             OK",
            "TimescaleDB extension (app)  2.28.2   2.17.0-2.30.1  OK",
            "Redis                        8.4.0    8.4.0          info",
        ])

    def test_uses_configured_runtime_and_only_reads(self):
        _, calls = self.rows(spec_config(), SPEC_OUTPUTS)
        self.assertTrue(calls)
        for args in calls:
            self.assertEqual(args[0], "podman")
            self.assertIn(args[1:3], (["exec", "openobserve"], ["exec", "db"], ["exec", "redis"],
                                      ["container", "inspect"]))
        self.assertIn(["podman", "exec", "redis", "redis-server", "--version"], calls)

    def test_inspects_each_container_once(self):
        _, calls = self.rows(spec_config(), SPEC_OUTPUTS)
        inspected = [args[-1] for args in calls if args[1] == "container"]
        self.assertEqual(sorted(inspected), ["db", "openobserve", "redis"])

    def test_container_not_running(self):
        rows, calls = self.rows(spec_config(), SPEC_OUTPUTS, {"db": "false"})
        db_rows = [r for r in rows if r.name.startswith(("Postgres", "TimescaleDB"))]
        self.assertEqual(db_rows, [
            dg.Row("Postgres", "-", "17", "CHECK", "container not running"),
            dg.Row("TimescaleDB extension (*)", "-", "2.17.0-2.30.1", "CHECK",
                   "container not running"),
        ])
        self.assertFalse(any(args[1] == "exec" and args[2] == "db" for args in calls))

    def test_container_not_found(self):
        rows, _ = self.rows(spec_config(), SPEC_OUTPUTS, {"redis": None})
        self.assertEqual(rows[-1], dg.Row("Redis", "-", "8.4.0", "CHECK", "container not found"))

    def test_inspect_failure(self):
        config = {"runtime": "docker", "check": [check(rule="info")]}
        runner = lambda args, timeout=None: (1, "permission denied while trying to connect\n")
        with mock.patch.object(dg, "run", runner):
            rows = dg.check_all(config)
        self.assertEqual(rows[0].result, "CHECK")
        self.assertIn("inspect failed: permission denied", rows[0].reason)

    def test_command_failed(self):
        outputs = {**SPEC_OUTPUTS, ("openobserve", "/openobserve", "--version"):
                   (126, "OCI runtime exec failed: permission denied\n")}
        rows, _ = self.rows(spec_config(), outputs)
        self.assertEqual(rows[0], dg.Row(
            "OpenObserve", "-", "0.92.2", "CHECK",
            "command failed (exit 126): OCI runtime exec failed: permission denied"))

    def test_version_not_found(self):
        outputs = {**SPEC_OUTPUTS, ("redis", "redis-server", "--version"): (0, "hello\n")}
        rows, _ = self.rows(spec_config(), outputs)
        self.assertEqual(rows[-1].result, "CHECK")
        self.assertEqual(rows[-1].reason, "version not found in output: hello")

    def test_empty_output_without_for_each(self):
        outputs = {**SPEC_OUTPUTS, ("redis", "redis-server", "--version"): (0, "  \n")}
        rows, _ = self.rows(spec_config(), outputs)
        self.assertEqual(rows[-1].reason, "command printed nothing")

    def test_unparseable_regex_capture(self):
        config = {"check": [check(rule="not_newer", pinned="1.0", version_regex=r"version=(\w+)")]}
        rows, _ = self.rows(config, {("c", "thing", "--version"): (0, "version=dev\n")})
        self.assertEqual(rows, [dg.Row("Thing", "dev", "1.0", "CHECK",
                                       "can't parse the running version")])

    def test_for_each_runs_per_item(self):
        outputs = {**SPEC_OUTPUTS,
                   ("db", *LIST_DBS): (0, "postgres\n  app  \n\nmetrics\n"),
                   ("db", "psql", "-U", "postgres", "-d", "metrics", "-tAc", EXT_SQL):
                       (0, "2.9.0\n")}
        rows, _ = self.rows(spec_config(), outputs)
        ts_rows = [r for r in rows if r.name.startswith("TimescaleDB")]
        self.assertEqual(ts_rows, [
            dg.Row("TimescaleDB extension (app)", "2.28.2", "2.17.0-2.30.1", "OK"),
            dg.Row("TimescaleDB extension (metrics)", "2.9.0", "2.17.0-2.30.1", "STOP",
                   "older than min; the new image can't run it"),
        ])

    def test_for_each_item_failure_is_check(self):
        outputs = {**SPEC_OUTPUTS,
                   ("db", "psql", "-U", "postgres", "-d", "app", "-tAc", EXT_SQL):
                       (2, 'psql: error: FATAL:  database "app" does not exist\n')}
        rows, _ = self.rows(spec_config(), outputs)
        ts_rows = [r for r in rows if r.name.startswith("TimescaleDB")]
        self.assertEqual([(r.name, r.result) for r in ts_rows],
                         [("TimescaleDB extension (app)", "CHECK")])

    def test_for_each_command_failed(self):
        outputs = {**SPEC_OUTPUTS, ("db", *LIST_DBS): (2, "psql: error: connection refused\n")}
        rows, _ = self.rows(spec_config(), outputs)
        self.assertIn(dg.Row("TimescaleDB extension (*)", "-", "2.17.0-2.30.1", "CHECK",
                             "for_each failed (exit 2): psql: error: connection refused"), rows)

    def test_for_each_no_items(self):
        outputs = {**SPEC_OUTPUTS, ("db", *LIST_DBS): (0, "\n")}
        rows, _ = self.rows(spec_config(), outputs)
        self.assertIn(dg.Row("TimescaleDB extension (*)", "-", "2.17.0-2.30.1", "info",
                             "for_each returned no items"), rows)

    def test_for_each_all_items_skipped(self):
        outputs = {**SPEC_OUTPUTS,
                   ("db", "psql", "-U", "postgres", "-d", "app", "-tAc", EXT_SQL): (0, "")}
        rows, _ = self.rows(spec_config(), outputs)
        self.assertIn(dg.Row("TimescaleDB extension (*)", "-", "2.17.0-2.30.1", "info",
                             "no version reported for any item"), rows)
        self.assertEqual(dg.exit_code(rows), dg.EXIT_STOP)  # OpenObserve still STOPs

    def test_item_substitution_is_literal(self):
        config = {"check": [check(name="DB {item}", rule="info", for_each=["list"],
                                  version_cmd=["show", "{item}", "{0} {x}"])]}
        rows, calls = self.rows(config, {
            ("c", "list"): (0, "a{b}\n"),
            ("c", "show", "a{b}", "{0} {x}"): (0, "1.2\n"),
        })
        self.assertEqual(rows, [dg.Row("DB a{b}", "1.2", "-", "info")])


class ExitCodeTest(unittest.TestCase):
    def code(self, *results):
        return dg.exit_code([dg.Row("x", "1", "1", r) for r in results])

    def test_exit_codes(self):
        self.assertEqual(self.code(), 0)
        self.assertEqual(self.code("OK", "info"), 0)
        self.assertEqual(self.code("OK", "STOP"), 1)
        self.assertEqual(self.code("OK", "CHECK"), 2)
        self.assertEqual(self.code("CHECK", "STOP", "OK"), 1)  # STOP wins

    def test_table_shows_check_reason(self):
        table = dg.format_table([dg.Row("Redis", "-", "8.4.0", "CHECK", "container not running")])
        self.assertEqual(table.splitlines()[1],
                         "Redis      -        8.4.0  CHECK container not running")


def run_main(config_text, execs, containers=None, extra_args=()):
    """Run main() on a temporary config with mocked containers -> (exit code, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "downgrade-guard.toml")
        with open(path, "w") as f:
            f.write(config_text)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(dg, "run", fake_run(execs, containers)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dg.main(["check", "-c", path, *extra_args])
    return code, out.getvalue(), err.getvalue()


class MainTest(unittest.TestCase):
    def test_stop(self):
        code, out, _ = run_main(SPEC_CONFIG, SPEC_OUTPUTS)
        self.assertEqual(code, 1)
        self.assertIn("OpenObserve", out)
        self.assertIn("Deploy blocked", out)

    def test_ok(self):
        outputs = {**SPEC_OUTPUTS, ("openobserve", "/openobserve", "--version"): (0, "v0.92.2")}
        code, out, _ = run_main(SPEC_CONFIG, outputs)
        self.assertEqual(code, 0)
        self.assertIn("All checks passed.", out)

    def test_unable_to_check(self):
        outputs = {**SPEC_OUTPUTS, ("openobserve", "/openobserve", "--version"): (0, "v0.92.2")}
        code, out, _ = run_main(SPEC_CONFIG, outputs, {"redis": "false"})
        self.assertEqual(code, 2)
        self.assertIn("couldn't be checked", out)

    def test_bad_config(self):
        code, out, err = run_main('[[check]]\nname = "A"\nrule = "newest"\n', {})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("'rule' must be one of", err)

    def test_missing_config(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = dg.main(["check", "-c", "/nonexistent.toml"])
        self.assertEqual(code, 2)
        self.assertIn("config file not found", err.getvalue())


class RunTest(unittest.TestCase):
    """The real subprocess wrapper, with harmless local commands."""

    def test_combines_stdout_and_stderr(self):
        code, out = dg.run([sys.executable, "-c",
                            "import sys; print('a'); sys.stderr.write('v1.2.3\\n')"])
        self.assertEqual(code, 0)
        self.assertIn("a", out)
        self.assertIn("v1.2.3", out)

    def test_missing_runtime(self):
        code, out = dg.run(["definitely-not-a-runtime-xyz", "exec"])
        self.assertEqual(code, 127)
        self.assertIn("command not found", out)

    def test_timeout(self):
        code, out = dg.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertEqual(code, 124)
        self.assertIn("timed out", out)



class JsonOutputTest(unittest.TestCase):
    def test_format_json(self):
        rows = [dg.Row("OpenObserve", "1.0.3", "0.92.2", "STOP",
                       "newer than pin; deploying would downgrade it"),
                dg.Row("Redis", "-", "-", "CHECK", "container not running")]
        report = json.loads(dg.format_json(rows, 1))
        self.assertEqual(report, {
            "tool_version": dg.__version__,
            "exit_code": 1,
            "results": [
                {"component": "OpenObserve", "running": "1.0.3", "pin": "0.92.2",
                 "result": "STOP", "reason": "newer than pin; deploying would downgrade it"},
                {"component": "Redis", "running": None, "pin": None,
                 "result": "CHECK", "reason": "container not running"},
            ],
        })

    def test_main_json(self):
        code, out, _ = run_main(SPEC_CONFIG, SPEC_OUTPUTS, extra_args=["--json"])
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual([r["result"] for r in report["results"]], ["STOP", "OK", "OK", "info"])
        self.assertEqual(report["results"][2]["component"], "TimescaleDB extension (app)")
        self.assertNotIn("error", report)

    def test_main_json_config_error(self):
        code, out, err = run_main('[[check]]\nname = "A"\n', {}, extra_args=["--json"])
        report = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["results"], [])
        self.assertIn("'rule' must be one of", report["error"])
        self.assertEqual(err, "")


class SplitImageTest(unittest.TestCase):
    def test_split(self):
        cases = {
            "redis:8.4.0-alpine": ("redis", "8.4.0-alpine"),
            "docker.io/library/redis:8.4.0": ("redis", "8.4.0"),
            "index.docker.io/library/redis:8": ("redis", "8"),
            "library/redis:8": ("redis", "8"),
            "Redis:8": ("redis", "8"),
            "bitnami/redis:7.2": ("bitnami/redis", "7.2"),
            "quay.io/keycloak/keycloak:26.4.0": ("quay.io/keycloak/keycloak", "26.4.0"),
            "localhost:5000/team/db:17.2": ("localhost:5000/team/db", "17.2"),
            "localhost:5000/team/db": ("localhost:5000/team/db", ""),
            "redis": ("redis", ""),
            "redis:8.4.0@sha256:abc123": ("redis", "8.4.0"),
            "redis@sha256:abc123": ("redis", ""),
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(dg.split_image(ref), expected)


class PinFromFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return path

    def pin(self, name, text, image, tag_regex=None, environ=None):
        return dg.pin_from_file(self.write(name, text), image, tag_regex, environ or {})

    def assertPinError(self, fragment, *args, **kwargs):
        with self.assertRaises(ValueError) as ctx:
            self.pin(*args, **kwargs)
        self.assertIn(fragment, str(ctx.exception))

    def test_dockerfile(self):
        dockerfile = "FROM node:22 AS build\nRUN make\nFROM docker.io/library/redis:8.4.0-alpine\n"
        self.assertEqual(self.pin("Dockerfile", dockerfile, "redis"), "8.4.0-alpine")
        self.assertEqual(self.pin("Dockerfile", dockerfile, "node"), "22")

    def test_dockerfile_flags_and_case(self):
        dockerfile = "from --platform=linux/amd64 postgres:17.2 as db\n"
        self.assertEqual(self.pin("Containerfile", dockerfile, "postgres"), "17.2")

    def test_dockerfile_build_args(self):
        dockerfile = ('ARG PG_MAJOR=17\nARG TS="2.30.1"\n'
                      "FROM timescale/timescaledb:${TS}-pg${PG_MAJOR}\n")
        self.assertEqual(self.pin("Dockerfile", dockerfile, "timescale/timescaledb"),
                         "2.30.1-pg17")
        self.assertEqual(self.pin("Dockerfile", dockerfile, "timescale/timescaledb",
                                  tag_regex=r"pg(\d+)"), "17")

    def test_dockerfile_ignores_environment(self):
        dockerfile = "ARG REDIS_VERSION=8.4.0\nFROM redis:$REDIS_VERSION\n"
        self.assertEqual(self.pin("Dockerfile", dockerfile, "redis",
                                  environ={"REDIS_VERSION": "9.9.9"}), "8.4.0")

    def test_compose(self):
        compose = """
services:
  db:
    image: "postgres:17.2"
  cache:
    image: 'redis:8.4.0-alpine'  # keep in sync
  auth:
    image: quay.io/keycloak/keycloak:26.4.0
"""
        self.assertEqual(self.pin("compose.yaml", compose, "postgres"), "17.2")
        self.assertEqual(self.pin("compose.yaml", compose, "redis"), "8.4.0-alpine")
        self.assertEqual(self.pin("compose.yaml", compose, "quay.io/keycloak/keycloak"), "26.4.0")
        self.assertPinError("not found", "compose.yaml", compose, "keycloak")

    def test_kubernetes_manifest(self):
        manifest = "spec:\n  containers:\n    - image: redis:8.4.0\n      name: redis\n"
        self.assertEqual(self.pin("deploy.yml", manifest, "redis"), "8.4.0")

    def test_compose_variables(self):
        compose = ("services:\n  a:\n    image: redis:${REDIS_TAG:-8.4.0}\n"
                   "  b:\n    image: postgres:${PG_TAG}\n")
        self.assertEqual(self.pin("compose.yaml", compose, "redis"), "8.4.0")
        self.assertEqual(self.pin("compose.yaml", compose, "redis",
                                  environ={"REDIS_TAG": "8.2.1"}), "8.2.1")
        self.assertEqual(self.pin("compose.yaml", compose, "postgres",
                                  environ={"PG_TAG": "17"}), "17")
        self.assertPinError("can't resolve $PG_TAG", "compose.yaml", compose, "postgres")

    def test_compose_dotenv(self):
        self.write(".env", "# versions\nexport PG_TAG='17.2'\nOTHER=x\n")
        compose = "services:\n  db:\n    image: postgres:${PG_TAG}\n"
        self.assertEqual(self.pin("compose.yaml", compose, "postgres"), "17.2")
        # the environment wins over .env, like docker compose
        self.assertEqual(self.pin("compose.yaml", compose, "postgres",
                                  environ={"PG_TAG": "16.4"}), "16.4")

    def test_unresolved_variable_in_name_is_a_hint(self):
        compose = "services:\n  db:\n    image: ${REGISTRY}/postgres:17\n"
        self.assertPinError("can't resolve $REGISTRY", "compose.yaml", compose, "postgres")

    def test_same_tag_twice_is_fine(self):
        compose = "services:\n  a:\n    image: redis:8.4.0\n  b:\n    image: redis:8.4.0\n"
        self.assertEqual(self.pin("compose.yaml", compose, "redis"), "8.4.0")

    def test_errors(self):
        self.assertPinError("different tags in", "compose.yaml",
                            "x:\n  image: redis:8.4.0\ny:\n  image: redis:8.2.0\n", "redis")
        self.assertPinError("has no tag", "compose.yaml", "x:\n  image: redis\n", "redis")
        self.assertPinError("has no tag", "Dockerfile", "FROM redis@sha256:abc\n", "redis")
        self.assertPinError("isn't a version", "compose.yaml", "x:\n  image: redis:latest\n",
                            "redis")
        self.assertPinError("isn't a version", "compose.yaml", "x:\n  image: redis:alpine\n",
                            "redis")
        self.assertPinError("tag_regex didn't match", "compose.yaml",
                            "x:\n  image: redis:8.4.0\n", "redis", tag_regex=r"pg(\d+)")
        self.assertPinError("not found", "Dockerfile", "FROM postgres:17\n", "redis")
        with self.assertRaisesRegex(ValueError, "can't read"):
            dg.pin_from_file(self.dir / "missing.yaml", "redis")

    def test_load_config_resolves_relative_to_config(self):
        (self.dir / "deploy").mkdir()
        self.write("deploy/compose.yaml", "services:\n  r:\n    image: redis:8.2.0-alpine\n")
        config = self.write("deploy/downgrade-guard.toml", """
[[check]]
name        = "Redis"
container   = "redis"
version_cmd = ["redis-server", "--version"]
rule        = "not_newer"
pinned_from = { file = "compose.yaml", image = "redis" }
""")
        loaded = dg.load_config(config)
        self.assertEqual(loaded["check"][0]["pinned"], "8.2.0-alpine")
        runner = fake_run({("redis", "redis-server", "--version"): (0, "Redis server v=8.4.0")})
        with mock.patch.object(dg, "run", runner):
            rows = dg.check_all(loaded)
        self.assertEqual(rows, [dg.Row("Redis", "8.4.0", "8.2.0-alpine", "STOP",
                                       "newer than pin; deploying would downgrade it")])

    def test_load_config_reports_pin_errors(self):
        config = self.write("downgrade-guard.toml", """
[[check]]
name        = "Redis"
container   = "redis"
version_cmd = ["redis-server", "--version"]
rule        = "not_newer"
pinned_from = { file = "nope.yaml", image = "redis" }
""")
        with self.assertRaisesRegex(dg.ConfigError, r"check #1 \(Redis\): pinned_from: can't read"):
            dg.load_config(config)


class PinnedFromValidationTest(unittest.TestCase):
    def errors(self, **fields):
        return dg.validate_config({"check": [check(**fields)]})

    def test_valid(self):
        spec = {"file": "compose.yaml", "image": "redis"}
        for rule in ("not_newer", "same_major", "same_minor", "info"):
            with self.subTest(rule=rule):
                self.assertEqual(self.errors(rule=rule, pinned_from=spec), [])
        self.assertEqual(self.errors(rule="same_major", pinned_from={**spec, "tag_regex": r"pg(\d+)"}),
                         [])

    def test_invalid(self):
        spec = {"file": "compose.yaml", "image": "redis"}
        cases = [
            ({"rule": "not_newer", "pinned_from": "compose.yaml"}, "must be a table"),
            ({"rule": "not_newer", "pinned_from": {"file": "compose.yaml"}},
             "'pinned_from.image' must be"),
            ({"rule": "not_newer", "pinned_from": {**spec, "tag": "x"}},
             "unknown field 'pinned_from.tag'"),
            ({"rule": "not_newer", "pinned": "1.0", "pinned_from": spec}, "not both"),
            ({"rule": "within", "min": "1", "max": "2", "pinned_from": spec}, "doesn't apply"),
            ({"rule": "not_newer", "pinned_from": {**spec, "tag_regex": "pg"}}, "capture group"),
            ({"rule": "not_newer"}, "needs 'pinned' (or 'pinned_from')"),
        ]
        for fields, fragment in cases:
            with self.subTest(fields=fields):
                errors = self.errors(**fields)
                self.assertTrue(any(fragment in e for e in errors), f"{fragment!r} not in {errors}")


def fake_kubectl(execs, gets=None):
    """A stand-in for dg.run with runtime = "kubectl".

    execs: {(target, *cmd): (exit code, output)}, cmd without "--".
    gets:  {target: (exit code, output)} for `kubectl get`; default is a running pod.
    """
    gets = gets or {}
    calls = []

    def run(args, timeout=None):
        calls.append(list(args))
        assert args[0] == "kubectl", args
        if "get" in args:
            target = args[args.index("get") + 1]
            return gets.get(target, (0, "Running"))
        assert "exec" in args, f"unexpected command {args}"
        target = args[args.index("exec") + 1]
        cmd = args[args.index("--") + 1:]
        return execs.get((target, *cmd), (1, "error: command not found\n"))

    run.calls = calls
    return run


class KubectlTest(unittest.TestCase):
    CONFIG = {"runtime": "kubectl", "namespace": "data", "context": "prod", "check": [
        check(name="Postgres", container="statefulset/db", pod_container="postgres",
              version_cmd=["psql", "-tAc", "show server_version"], rule="same_major", pinned="17"),
        check(name="Redis", container="redis-0", version_cmd=["redis-server", "--version"],
              rule="not_newer", pinned="8.4.0"),
    ]}

    def rows(self, execs, gets=None, config=None):
        runner = fake_kubectl(execs, gets)
        with mock.patch.object(dg, "run", runner):
            return dg.check_all(config or self.CONFIG), runner.calls

    def test_exec_and_get_commands(self):
        rows, calls = self.rows({
            ("statefulset/db", "psql", "-tAc", "show server_version"): (0, "17.11\n"),
            ("redis-0", "redis-server", "--version"): (0, "Redis server v=8.4.0\n"),
        })
        self.assertEqual([r.result for r in rows], ["OK", "OK"])
        self.assertEqual(calls, [
            ["kubectl", "--context", "prod", "--namespace", "data", "get", "statefulset/db",
             "-o", "jsonpath={.status.phase}"],
            ["kubectl", "--context", "prod", "--namespace", "data", "exec", "statefulset/db",
             "-c", "postgres", "--", "psql", "-tAc", "show server_version"],
            ["kubectl", "--context", "prod", "--namespace", "data", "get", "pod/redis-0",
             "-o", "jsonpath={.status.phase}"],
            ["kubectl", "--context", "prod", "--namespace", "data", "exec", "redis-0",
             "--", "redis-server", "--version"],
        ])

    def test_without_namespace_or_context(self):
        config = {"runtime": "kubectl", "check": [check(rule="info")]}
        _, calls = self.rows({("c", "thing", "--version"): (0, "1.2\n")}, config=config)
        self.assertEqual(calls[1], ["kubectl", "exec", "c", "--", "thing", "--version"])

    def test_not_found_and_not_running(self):
        rows, calls = self.rows({}, gets={
            "statefulset/db": (1, 'Error from server (NotFound): statefulsets.apps "db" not found\n'),
            "pod/redis-0": (0, "Pending"),
        })
        self.assertEqual([(r.result, r.reason) for r in rows], [
            ("CHECK", "statefulset/db not found"),
            ("CHECK", "pod not running (Pending)"),
        ])
        self.assertFalse(any("exec" in args for args in calls))

    def test_get_failure(self):
        rows, _ = self.rows({}, gets={"statefulset/db": (1, "error: You must be logged in\n"),
                                      "pod/redis-0": (1, "error: You must be logged in\n")})
        self.assertEqual(rows[0].reason, "kubectl get failed: error: You must be logged in")

    def test_kubectl_log_lines_are_dropped(self):
        klog = ('E1005 22:47:25.143104 1281526 memcache.go:381] "Couldn\'t get current server '
                'API group list" err="dial tcp 127.0.0.1:1: connect: connection refused"\n')
        refused = "The connection to the server 127.0.0.1:1 was refused\n"
        rows, _ = self.rows({}, gets={"statefulset/db": (1, klog * 3 + refused),
                                      "pod/redis-0": (0, "Running")})
        self.assertEqual(rows[0].reason,
                         "kubectl get failed: The connection to the server 127.0.0.1:1 was refused")
        rows, _ = self.rows({("redis-0", "redis-server", "--version"):
                             (0, "W1005 10:00:00.000001 7 warnings.go:70] v1.2.3 is deprecated\n"
                                 "Redis server v=8.4.0\n")}, gets={"statefulset/db": (1, refused)})
        self.assertEqual(rows[1], dg.Row("Redis", "8.4.0", "8.4.0", "OK"))

    def test_defaulted_container_notice_is_dropped(self):
        notice = 'Defaulted container "app" out of: app, sidecar-1.2.3\n'
        config = {"runtime": "kubectl", "check": [check(
            name="DB ({item})", container="db-0", rule="info", for_each=["list"],
            version_cmd=["show", "{item}"])]}
        rows, _ = self.rows({
            ("db-0", "list"): (0, notice + "app\n"),
            ("db-0", "show", "app"): (0, notice + "2.28.2\n"),
        }, config=config)
        self.assertEqual(rows, [dg.Row("DB (app)", "2.28.2", "-", "info")])

    def test_validation(self):
        docker_with_kube_fields = {"runtime": "docker", "namespace": "x",
                                   "check": [check(rule="info", pod_container="y")]}
        errors = dg.validate_config(docker_with_kube_fields)
        self.assertTrue(any("'namespace' only applies" in e for e in errors), errors)
        self.assertTrue(any("'pod_container' only applies" in e for e in errors), errors)
        self.assertEqual(dg.validate_config(self.CONFIG), [])
        bad = {"runtime": "kubectl", "context": "", "check": [check(rule="info")]}
        self.assertTrue(any("'context' must be" in e for e in dg.validate_config(bad)))

    def test_example_is_valid(self):
        dg.load_config(ROOT / "examples" / "kubernetes.toml")


if __name__ == "__main__":
    unittest.main()
