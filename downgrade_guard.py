#!/usr/bin/env python3
"""downgrade-guard: block a deploy that would downgrade a stateful service.

Reads the versions actually running in containers, compares them with the
versions about to be deployed, and exits non-zero if any stateful service
would be downgraded.

Read-only: the only commands it runs are `<runtime> container inspect` or
`kubectl get` (to see whether a container is up) and the version commands from
your config, via `<runtime> exec` or `kubectl exec`.

Usage:  python downgrade_guard.py check [-c downgrade-guard.toml] [--json]
Exit:   0 = all OK, 1 = at least one STOP, 2 = something couldn't be checked
"""

import argparse
import collections
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib
from typing import NamedTuple

__version__ = "0.2.0"

EXIT_OK, EXIT_STOP, EXIT_UNCHECKED = 0, 1, 2
RUNTIMES = ("docker", "podman", "kubectl")
KUBECTL_FIELDS = ("namespace", "context")
TOP_LEVEL_FIELDS = {"runtime", "check", *KUBECTL_FIELDS}
RULE_FIELDS = {  # rule -> fields it requires
    "not_newer": ("pinned",),
    "within": ("min", "max"),
    "same_major": ("pinned",),
    "same_minor": ("pinned",),
    "info": (),
}
CHECK_FIELDS = {"name", "container", "pod_container", "version_cmd", "version_regex",
                "for_each", "rule", "pinned", "pinned_from", "min", "max"}
PINNED_FROM_FIELDS = {"file", "image", "tag_regex"}
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


# --- Pins from Dockerfiles and compose files ----------------------------------

FROM_RE = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.IGNORECASE)
ARG_RE = re.compile(r"""^\s*ARG\s+(\w+)=["']?([^"'\s]*)""", re.IGNORECASE)
IMAGE_RE = re.compile(r"""^\s*(?:-\s+)?image:\s*["']?([^"'\s#]+)""")
VAR_RE = re.compile(r"\$\{(\w+)(?:(:?[-?])([^}]*))?\}|\$(\w+)")


def normalize_image(name):
    """'docker.io/library/Redis' -> 'redis', so equivalent Docker Hub names compare equal."""
    name = name.lower()
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.removeprefix("library/")


def split_image(ref):
    """'docker.io/library/redis:8.4.0-alpine@sha256:...' -> ('redis', '8.4.0-alpine')."""
    ref = ref.split("@", 1)[0]
    colon = ref.rfind(":")
    if colon > ref.rfind("/"):  # a colon before the last slash is a registry port
        return normalize_image(ref[:colon]), ref[colon + 1:]
    return normalize_image(ref), ""


def _expand(text, variables):
    """Substitute $VAR, ${VAR} and ${VAR:-default}. Returns (text, unresolved names)."""
    missing = []

    def substitute(m):
        name = m[1] or m[4]
        if variables.get(name):
            return variables[name]
        if m[2] in ("-", ":-"):
            return m[3]
        missing.append(name)
        return ""

    return VAR_RE.sub(substitute, text), missing


def _read_dotenv(path):
    """KEY=VALUE lines from a compose-style .env file (empty if there isn't one)."""
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


def pin_from_file(path, image, tag_regex=None, environ=None):
    """Find the tag `image` is pinned to in a Dockerfile or a compose/Kubernetes YAML file.

    Dockerfiles: `FROM` lines, with `${VAR}` taken from `ARG VAR=default` lines.
    YAML files: `image:` lines, with `${VAR}` taken from the environment, then a
    `.env` file next to it (as docker compose does). `${VAR:-default}` works in both.
    Returns the tag (or tag_regex's first group). Raises ValueError with the reason.
    """
    path = pathlib.Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise ValueError(f"can't read {path}: {e.strerror}") from None

    is_yaml = path.suffix.lower() in (".yaml", ".yml")
    build_args = {}
    if is_yaml:
        environ = os.environ if environ is None else environ
        variables = collections.ChainMap(environ, _read_dotenv(path.parent / ".env"))
    else:
        variables = build_args
    pattern = IMAGE_RE if is_yaml else FROM_RE

    want = normalize_image(image)
    tags, unresolved = set(), []
    for number, line in enumerate(lines, 1):
        if not is_yaml and (m := ARG_RE.match(line)):
            build_args[m[1]] = _expand(m[2], build_args)[0]
            continue
        if not (m := pattern.match(line)):
            continue
        ref, missing = _expand(m[1], variables)
        problem = f"line {number}: can't resolve ${missing[0]} in {m[1]!r}" if missing else ""
        name, tag = split_image(ref)
        if name == want:
            if problem:
                raise ValueError(problem)
            tags.add(tag)
        elif problem:
            unresolved.append(problem)

    if not tags:
        hint = f" ({unresolved[0]})" if unresolved else ""
        raise ValueError(f"image {image!r} not found in {path}{hint}")
    if len(tags) > 1:
        found = ", ".join(sorted(t or "no tag" for t in tags))
        raise ValueError(f"image {image!r} has different tags in {path}: {found}")
    tag = tags.pop()
    if not tag:
        raise ValueError(f"image {image!r} has no tag in {path}; pin a version")
    pin = tag
    if tag_regex:
        m = re.search(tag_regex, tag)
        if not m:
            raise ValueError(f"tag_regex didn't match tag {tag!r} of {image!r}")
        pin = m.group(1)
    try:
        parse_version(pin)
    except ValueError:
        raise ValueError(f"tag {pin!r} of {image!r} in {path} isn't a version; pin one") from None
    return pin


# --- Config -------------------------------------------------------------------

def load_config(path):
    """Read, validate and resolve the TOML config. Raises ConfigError with every problem found."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as e:
        raise ConfigError(f"can't read {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    errors = validate_config(data) or resolve_pins(data, pathlib.Path(path).parent)
    if errors:
        raise ConfigError(f"{path}:\n" + "\n".join(f"  - {e}" for e in errors))
    data.setdefault("runtime", "docker")
    return data


def resolve_pins(config, base_dir):
    """Fill in `pinned` for checks that use pinned_from. Returns a list of problems."""
    errors = []
    for i, check in enumerate(config["check"], 1):
        spec = check.get("pinned_from")
        if spec is None:
            continue
        try:
            check["pinned"] = pin_from_file(base_dir / spec["file"], spec["image"],
                                            spec.get("tag_regex"))
        except ValueError as e:
            errors.append(f"check #{i} ({check['name']}): pinned_from: {e}")
    return errors


def validate_config(data):
    """Return a list of human-readable problems (empty if the config is valid)."""
    errors = [f"unknown top-level key {key!r}" for key in sorted(data.keys() - TOP_LEVEL_FIELDS)]
    runtime = data.get("runtime", "docker")
    if runtime not in RUNTIMES:
        errors.append(f"runtime must be one of {', '.join(RUNTIMES)}; got {runtime!r}")
    for key in KUBECTL_FIELDS:
        if key not in data:
            continue
        if runtime != "kubectl":
            errors.append(f"{key!r} only applies to runtime = \"kubectl\"")
        elif not _is_text(data[key]):
            errors.append(f"{key!r} must be a non-empty string")
    checks = data.get("check")
    if not isinstance(checks, list) or not checks or not all(isinstance(c, dict) for c in checks):
        errors.append("no checks defined; add at least one [[check]] table")
        return errors
    for i, check in enumerate(checks, 1):
        name = check.get("name")
        label = f"check #{i}" + (f" ({name})" if isinstance(name, str) and name else "")
        errors += [f"{label}: {e}" for e in _check_errors(check, runtime)]
    return errors


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


def _is_command(value):
    return isinstance(value, list) and value and all(isinstance(a, str) for a in value)


def _regex_errors(regex, key):
    if not isinstance(regex, str):
        return [f"{key!r} must be a string"]
    try:
        if re.compile(regex).groups < 1:
            return [f"{key!r} needs a capture group around the version"]
    except re.error as e:
        return [f"{key!r} is not a valid regex: {e}"]
    return []


def _check_errors(check, runtime="docker"):
    errors = [f"unknown field {key!r}" for key in sorted(check.keys() - CHECK_FIELDS)]

    for key in ("name", "container"):
        if not _is_text(check.get(key)):
            errors.append(f"{key!r} must be a non-empty string")
    if "pod_container" in check:
        if runtime != "kubectl":
            errors.append("'pod_container' only applies to runtime = \"kubectl\"")
        elif not _is_text(check["pod_container"]):
            errors.append("'pod_container' must be a non-empty string")
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
        for key in RULE_FIELDS[rule]:
            if key == "pinned" and "pinned_from" not in check and key not in check:
                errors.append(f"rule {rule!r} needs 'pinned' (or 'pinned_from')")
            elif key != "pinned" and key not in check:
                errors.append(f"rule {rule!r} needs {key!r}")

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
        errors += _regex_errors(check["version_regex"], "version_regex")
    if "pinned_from" in check:
        errors += _pinned_from_errors(check, rule)
    return errors


def _pinned_from_errors(check, rule):
    spec = check["pinned_from"]
    if not isinstance(spec, dict):
        return ["'pinned_from' must be a table, "
                "e.g. pinned_from = { file = \"compose.yaml\", image = \"redis\" }"]
    errors = [f"unknown field 'pinned_from.{key}'"
              for key in sorted(spec.keys() - PINNED_FROM_FIELDS)]
    for key in ("file", "image"):
        if not _is_text(spec.get(key)):
            errors.append(f"'pinned_from.{key}' must be a non-empty string")
    if "pinned" in check:
        errors.append("use either 'pinned' or 'pinned_from', not both")
    if rule == "within":
        errors.append("'pinned_from' doesn't apply to rule 'within' (it uses 'min' and 'max')")
    if "tag_regex" in spec:
        errors += _regex_errors(spec["tag_regex"], "pinned_from.tag_regex")
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


def _kubectl(config):
    """`kubectl` plus the configured --context and --namespace flags."""
    args = ["kubectl"]
    if "context" in config:
        args += ["--context", config["context"]]
    if "namespace" in config:
        args += ["--namespace", config["namespace"]]
    return args


KLOG_LINE_RE = re.compile(r"^[IWEF]\d{4} \d\d:\d\d:\d\d\.\d+\s+\d+ \S+:\d+\] ")


def _without_kubectl_noise(output):
    """Drop kubectl's own log lines and its "Defaulted container" notice; keep everything else."""
    lines = [line for line in output.splitlines()
             if not line.startswith("Defaulted container ") and not KLOG_LINE_RE.match(line)]
    return "\n".join(lines)


def container_problem(config, container):
    """None if the container is running, otherwise the reason it can't be checked."""
    runtime = config.get("runtime", "docker")
    if runtime == "kubectl":
        target = container if "/" in container else f"pod/{container}"
        rc, out = run([*_kubectl(config), "get", target, "-o", "jsonpath={.status.phase}"])
        out = _without_kubectl_noise(out)
        if rc != 0:
            if "(NotFound)" in out:
                return f"{target} not found"
            return f"kubectl get failed: {_snippet(out)}"
        is_pod = target.split("/", 1)[0] in ("pod", "pods", "po")
        if is_pod and out.strip() != "Running":
            return f"pod not running ({out.strip() or 'unknown phase'})"
        return None

    rc, out = run([runtime, "container", "inspect", "--format", "{{.State.Running}}", container])
    if rc != 0:
        if "no such" in out.lower():
            return "container not found"
        return f"inspect failed: {_snippet(out)}"
    if out.strip() != "true":
        return "container not running"
    return None


def exec_in(config, check, cmd):
    """Run cmd inside the check's container -> (exit code, output)."""
    runtime = config.get("runtime", "docker")
    if runtime != "kubectl":
        return run([runtime, "exec", check["container"], *cmd])
    args = [*_kubectl(config), "exec", check["container"]]
    if "pod_container" in check:
        args += ["-c", check["pod_container"]]
    rc, out = run([*args, "--", *cmd])
    return rc, _without_kubectl_noise(out)


def _check_item(config, check, name, cmd, pin, skip_empty):
    """Run one version command and judge it. Returns a Row, or None if skipped."""
    rc, out = exec_in(config, check, cmd)
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


def run_check(config, check):
    """Run one [[check]] (expanding for_each) and return its table rows."""
    name, pin = check["name"], pin_label(check)
    if "for_each" not in check:
        return [_check_item(config, check, name, check["version_cmd"], pin, skip_empty=False)]

    group_name = name.replace("{item}", "*")
    rc, out = exec_in(config, check, check["for_each"])
    if rc != 0:
        return [Row(group_name, "-", pin, "CHECK", f"for_each failed (exit {rc}): {_snippet(out)}")]
    items = [line.strip() for line in out.splitlines() if line.strip()]
    rows = []
    for item in items:
        cmd = [arg.replace("{item}", item) for arg in check["version_cmd"]]
        row = _check_item(config, check, name.replace("{item}", item), cmd, pin, skip_empty=True)
        if row:
            rows.append(row)
    if not rows:
        reason = "for_each returned no items" if not items else "no version reported for any item"
        rows.append(Row(group_name, "-", pin, "info", reason))
    return rows


def check_all(config):
    """Run every check in the config and return all rows."""
    problems = {}  # container -> reason it can't be checked (None if running)
    rows = []
    for check in config["check"]:
        container = check["container"]
        if container not in problems:
            problems[container] = container_problem(config, container)
        if problems[container]:
            rows.append(Row(check["name"].replace("{item}", "*"), "-", pin_label(check),
                            "CHECK", problems[container]))
        else:
            rows += run_check(config, check)
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


def format_json(rows, code, error=None):
    def value(text):
        return None if text in ("", "-") else text

    report = {
        "tool_version": __version__,
        "exit_code": code,
        "results": [{"component": row.name, "running": value(row.running),
                     "pin": value(row.pin), "result": row.result, "reason": value(row.reason)}
                    for row in rows],
    }
    if error:
        report["error"] = error
    return json.dumps(report, indent=2)


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
    check_cmd.add_argument("--json", action="store_true",
                           help="print the results as JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        if args.json:
            print(format_json([], EXIT_UNCHECKED, error=str(e)))
        else:
            print(f"downgrade-guard: {e}", file=sys.stderr)
        return EXIT_UNCHECKED

    rows = check_all(config)
    code = exit_code(rows)
    if args.json:
        print(format_json(rows, code))
    else:
        print(format_table(rows))
        print()
        print(summary(rows))
    return code


if __name__ == "__main__":
    sys.exit(main())
