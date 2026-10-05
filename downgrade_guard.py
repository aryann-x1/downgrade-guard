#!/usr/bin/env python3
"""downgrade-guard: block a deploy that would downgrade a stateful service.

Reads the versions actually running in containers, compares them with the
versions about to be deployed, and exits non-zero if any stateful service
would be downgraded.

Read-only: the only commands it runs are `<runtime> container inspect` (to see
whether a container is up) and the version commands from your config, via
`<runtime> exec`.

Usage:  python downgrade_guard.py check [-c downgrade-guard.toml]
Exit:   0 = all OK, 1 = at least one STOP, 2 = something couldn't be checked
"""

import argparse
import re
import subprocess
import sys
import tomllib
from typing import NamedTuple

__version__ = "0.1.0"

EXIT_OK, EXIT_STOP, EXIT_UNCHECKED = 0, 1, 2
RUNTIMES = ("docker", "podman")
RULE_FIELDS = {  # rule -> fields it requires
    "not_newer": ("pinned",),
    "within": ("min", "max"),
    "same_major": ("pinned",),
    "same_minor": ("pinned",),
    "info": (),
}
CHECK_FIELDS = {"name", "container", "version_cmd", "version_regex", "for_each",
                "rule", "pinned", "min", "max"}
DEFAULT_VERSION_REGEX = r"v?(\d+(?:\.\d+)+)"
COMMAND_TIMEOUT = 30  # seconds per command


class ConfigError(Exception):
    """The config file is missing or invalid."""


class Row(NamedTuple):
    name: str
    running: str
    pin: str
    result: str  # OK, STOP, CHECK or info
    reason: str = ""


# --- Versions -----------------------------------------------------------------

def parse_version(text):
    """'v8.4.0-alpine' -> (8, 4, 0). Raises ValueError if there's no leading number."""
    m = re.match(r"[vV]?(\d+(?:\.\d+)*)", text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(part) for part in m.group(1).split("."))


def _padded(version, length):
    return version + (0,) * (length - len(version))


def compare_versions(a, b):
    """Return -1, 0 or 1 for a <, ==, > b. Missing parts count as zero (17 == 17.0)."""
    va, vb = parse_version(a), parse_version(b)
    n = max(len(va), len(vb))
    va, vb = _padded(va, n), _padded(vb, n)
    return (va > vb) - (va < vb)


def extract_version(output, regex=None):
    """Find the version in a command's output: first capture group of `regex`, or None."""
    m = re.search(regex or DEFAULT_VERSION_REGEX, output)
    return m.group(1).strip() if m else None


# --- Rules --------------------------------------------------------------------

def evaluate(check, running):
    """Apply the check's rule to the running version -> (result, reason)."""
    rule = check["rule"]
    if rule == "info":
        return "info", ""
    if rule == "within":
        if compare_versions(running, check["min"]) < 0:
            return "STOP", "older than min; the new image can't run it"
        if compare_versions(running, check["max"]) > 0:
            return "STOP", "newer than max; deploying would downgrade it"
        return "OK", ""
    pinned = check["pinned"]
    if rule == "not_newer":
        cmp = compare_versions(running, pinned)
        if cmp > 0:
            return "STOP", "newer than pin; deploying would downgrade it"
        if cmp < 0:
            return "OK", "older than pin; deploy will upgrade"
        return "OK", ""
    # same_major / same_minor
    parts, label = (1, "major") if rule == "same_major" else (2, "minor")
    run_v = _padded(parse_version(running), parts)[:parts]
    pin_v = _padded(parse_version(pinned), parts)[:parts]
    if run_v > pin_v:
        return "STOP", f"{label} newer than pin; deploying would downgrade it"
    if run_v < pin_v:
        return "STOP", f"{label} older than pin; needs a planned {label} upgrade"
    return "OK", ""


def pin_label(check):
    if check["rule"] == "within":
        return f"{check['min']}-{check['max']}"
    return check.get("pinned", "-")


# --- Config -------------------------------------------------------------------

def load_config(path):
    """Read and validate the TOML config. Raises ConfigError with every problem found."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as e:
        raise ConfigError(f"can't read {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    errors = validate_config(data)
    if errors:
        raise ConfigError(f"{path}:\n" + "\n".join(f"  - {e}" for e in errors))
    data.setdefault("runtime", "docker")
    return data


def validate_config(data):
    """Return a list of human-readable problems (empty if the config is valid)."""
    errors = [f"unknown top-level key {key!r}" for key in sorted(data.keys() - {"runtime", "check"})]
    runtime = data.get("runtime", "docker")
    if runtime not in RUNTIMES:
        errors.append(f"runtime must be one of {', '.join(RUNTIMES)}; got {runtime!r}")
    checks = data.get("check")
    if not isinstance(checks, list) or not checks or not all(isinstance(c, dict) for c in checks):
        errors.append("no checks defined; add at least one [[check]] table")
        return errors
    for i, check in enumerate(checks, 1):
        name = check.get("name")
        label = f"check #{i}" + (f" ({name})" if isinstance(name, str) and name else "")
        errors += [f"{label}: {e}" for e in _check_errors(check)]
    return errors


def _is_command(value):
    return isinstance(value, list) and value and all(isinstance(a, str) for a in value)


def _check_errors(check):
    errors = [f"unknown field {key!r}" for key in sorted(check.keys() - CHECK_FIELDS)]

    for key in ("name", "container"):
        value = check.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key!r} must be a non-empty string")
    if not _is_command(check.get("version_cmd")):
        errors.append("'version_cmd' must be a non-empty list of strings")
    if "for_each" in check and not _is_command(check["for_each"]):
        errors.append("'for_each' must be a non-empty list of strings")
    elif "for_each" not in check:
        texts = [check.get("name")] + list(check.get("version_cmd") or [])
        if any(isinstance(t, str) and "{item}" in t for t in texts):
            errors.append("uses {item} but has no 'for_each'")

    rule = check.get("rule")
    if rule not in RULE_FIELDS:
        errors.append(f"'rule' must be one of {', '.join(RULE_FIELDS)}; got {rule!r}")
    else:
        errors += [f"rule {rule!r} needs {key!r}" for key in RULE_FIELDS[rule] if key not in check]

    valid_versions = {}
    for key in ("pinned", "min", "max"):
        if key not in check:
            continue
        value = check[key]
        if not isinstance(value, str):
            errors.append(f"{key!r} must be a quoted string, e.g. {key} = \"{value}\"")
            continue
        try:
            parse_version(value)
            valid_versions[key] = value
        except ValueError:
            errors.append(f"{key!r} is not a version: {value!r}")
    if rule == "within" and "min" in valid_versions and "max" in valid_versions:
        if compare_versions(valid_versions["min"], valid_versions["max"]) > 0:
            errors.append("'min' is greater than 'max'")

    if "version_regex" in check:
        regex = check["version_regex"]
        if not isinstance(regex, str):
            errors.append("'version_regex' must be a string")
        else:
            try:
                if re.compile(regex).groups < 1:
                    errors.append("'version_regex' needs a capture group around the version")
            except re.error as e:
                errors.append(f"'version_regex' is not a valid regex: {e}")
    return errors


# --- Running checks -----------------------------------------------------------

def run(args, timeout=COMMAND_TIMEOUT):
    """Run a command and return (exit code, stdout+stderr). The only place that touches subprocess."""
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace",
                              timeout=timeout)
    except FileNotFoundError:
        return 127, f"{args[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return proc.returncode, proc.stdout


def _snippet(output, limit=80):
    """First non-empty line of output, shortened for the table."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    text = lines[0] if lines else "no output"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def container_problem(runtime, container):
    """None if the container is running, otherwise the reason it can't be checked."""
    rc, out = run([runtime, "container", "inspect", "--format", "{{.State.Running}}", container])
    if rc != 0:
        if "no such" in out.lower():
            return "container not found"
        return f"inspect failed: {_snippet(out)}"
    if out.strip() != "true":
        return "container not running"
    return None


def _check_item(runtime, check, name, cmd, pin, skip_empty):
    """Run one version command and judge it. Returns a Row, or None if skipped."""
    rc, out = run([runtime, "exec", check["container"], *cmd])
    if rc != 0:
        return Row(name, "-", pin, "CHECK", f"command failed (exit {rc}): {_snippet(out)}")
    if not out.strip():
        return None if skip_empty else Row(name, "-", pin, "CHECK", "command printed nothing")
    running = extract_version(out, check.get("version_regex"))
    if running is None:
        return Row(name, "-", pin, "CHECK", f"version not found in output: {_snippet(out)}")
    try:
        result, reason = evaluate(check, running)
    except ValueError:
        return Row(name, running, pin, "CHECK", "can't parse the running version")
    return Row(name, running, pin, result, reason)


def run_check(runtime, check):
    """Run one [[check]] (expanding for_each) and return its table rows."""
    name, pin = check["name"], pin_label(check)
    if "for_each" not in check:
        return [_check_item(runtime, check, name, check["version_cmd"], pin, skip_empty=False)]

    group_name = name.replace("{item}", "*")
    rc, out = run([runtime, "exec", check["container"], *check["for_each"]])
    if rc != 0:
        return [Row(group_name, "-", pin, "CHECK", f"for_each failed (exit {rc}): {_snippet(out)}")]
    items = [line.strip() for line in out.splitlines() if line.strip()]
    rows = []
    for item in items:
        cmd = [arg.replace("{item}", item) for arg in check["version_cmd"]]
        row = _check_item(runtime, check, name.replace("{item}", item), cmd, pin, skip_empty=True)
        if row:
            rows.append(row)
    if not rows:
        reason = "for_each returned no items" if not items else "no version reported for any item"
        rows.append(Row(group_name, "-", pin, "info", reason))
    return rows


def check_all(config):
    """Run every check in the config and return all rows."""
    runtime = config.get("runtime", "docker")
    problems = {}  # container -> reason it can't be checked (None if running)
    rows = []
    for check in config["check"]:
        container = check["container"]
        if container not in problems:
            problems[container] = container_problem(runtime, container)
        if problems[container]:
            rows.append(Row(check["name"].replace("{item}", "*"), "-", pin_label(check),
                            "CHECK", problems[container]))
        else:
            rows += run_check(runtime, check)
    return rows


# --- Output -------------------------------------------------------------------

def exit_code(rows):
    results = {row.result for row in rows}
    if "STOP" in results:
        return EXIT_STOP
    if "CHECK" in results:
        return EXIT_UNCHECKED
    return EXIT_OK


def format_table(rows):
    headers = ("COMPONENT", "RUNNING", "PIN")
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]

    def line(cols, result):
        return ("  ".join(c.ljust(w) for c, w in zip(cols, widths)) + "  " + result).rstrip()

    out = [line(headers, "RESULT")]
    out += [line(row[:3], f"{row.result:<5} {row.reason}") for row in rows]
    return "\n".join(out)


def summary(rows):
    stops = sum(row.result == "STOP" for row in rows)
    unchecked = sum(row.result == "CHECK" for row in rows)
    lines = []
    if stops:
        lines.append(f"STOP: {stops} component(s) would be downgraded or can't run on the "
                     "versions being deployed. Deploy blocked.")
    if unchecked:
        lines.append(f"CHECK: {unchecked} component(s) couldn't be checked. Not safe to assume OK.")
    return "\n".join(lines) or "All checks passed."


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="downgrade_guard.py",
        description="Block deploys that would downgrade stateful services.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    check_cmd = commands.add_parser(
        "check", help="compare running versions with the versions about to be deployed")
    check_cmd.add_argument("-c", "--config", default="downgrade-guard.toml",
                           help="path to the config file (default: ./downgrade-guard.toml)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"downgrade-guard: {e}", file=sys.stderr)
        return EXIT_UNCHECKED

    rows = check_all(config)
    print(format_table(rows))
    print()
    print(summary(rows))
    return exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
