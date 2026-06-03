#!/usr/bin/env python3
"""contract-vault — a git-backed register for SIGNED contracts: register executed
deals, then search the portfolio and surface renewals, notice deadlines, and
obligations as a calendar. Local-first, single-file, stdlib-only.

"obligations" is a VIEW over the register, not a separate tool.

Stands alone, but composes with the contract-ops CLI suite (it ingests extract-cli
output and shares the suite's agent conventions), sitting at the manage-out end of:

    extract (entry) -> draft / review / compare / convert / sign (core) -> contract-vault (manage-out)

It is the post-signature sibling of ``template-vault`` (which stores BLANKS — clauses,
variables, composition); contract-vault stores SIGNED INSTANCES (parties, dates,
obligations) in the same git-backed, single-file, subcommand-rich shape.

contract-vault does NOT extract documents itself. ``ingest`` consumes the JSON emitted
by ``extract-cli`` (https://github.com/DrBaher/extract-cli) -- either by shelling out to
``extract <file> --json`` when it is on PATH, or by reading piped JSON on stdin. Every
field that extract emits carries a confidence and a source; contract-vault stores and
propagates those, treating data as "verify, not trust".

Stdlib-only. No third-party runtime dependencies. LLM use is opt-in and delegated to the
extract step (never reimplemented and never on a hot path); the .ics / reminder / register
logic is purely deterministic and works fully with the LLM off.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

__version__ = "0.5.2"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECORD_SCHEMA_VERSION = "1.0"
VAULT_CONFIG_NAME = ".contract-vault.json"
VAULT_KIND = "executed-contract-vault"
RECORD_FILENAME = "record.json"
DEFAULT_WITHIN_DAYS = 90

# Field-provenance markers. extract emits {deterministic, llm, none}; contract-vault adds
# "manual" for human-entered/edited values. Treated as "verify, not trust" downstream.
SOURCE_DETERMINISTIC = "deterministic"
SOURCE_LLM = "llm"
SOURCE_MANUAL = "manual"
SOURCE_NONE = "none"

# Exit codes (suite convention): 0 ok, 1 generic failure / findings, 2 bad usage.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# Upper bound (in days, ~100 years) for any user/record-supplied day count that is
# later fed to dt.timedelta or date arithmetic. timedelta and dt.date overflow well
# beyond this; clamping keeps malformed/hand-edited input from crashing date math.
MAX_HORIZON_DAYS = 36500

# Output state, configured once in main() from flags + environment.
_NO_COLOR = False
_QUIET = False

JSONObj = Dict[str, Any]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VaultError(Exception):
    """User-actionable error; printed to stderr, exits 1."""


class NotFoundError(VaultError):
    """A requested resource (vault, deal, field) does not exist."""


class UsageError(VaultError):
    """Bad invocation; exits 2."""


# ---------------------------------------------------------------------------
# Output helpers (color, json, stderr, --why)
# ---------------------------------------------------------------------------


def _configure_streams() -> None:
    """Force UTF-8 on stdout/stderr regardless of locale, and disable newline translation.

    newline="" stops Windows text mode from rewriting "\\n" to "\\r\\n" — which would
    otherwise turn the RFC 5545 "\\r\\n" line endings emitted by `due --format ics` into
    "\\r\\r\\n" and corrupt the calendar. On POSIX this is a no-op.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", newline="")
            except (ValueError, OSError):
                pass


def _harden(path: Path, mode: int) -> None:
    """Best-effort restrictive permissions on a vault path (owner-only). POSIX-effective;
    a no-op-ish on Windows/odd filesystems. Never raises."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def _atomic_write_text(path: Path, data: str, *, mode: Optional[int] = None) -> None:
    """Write text atomically: a sibling temp file + os.replace, so a crash mid-write
    never leaves a truncated/partial record.json or .contract-vault.json. os.replace
    is atomic on the same filesystem (POSIX + Windows)."""
    directory = path.parent
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            _harden(Path(tmp), mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rmtree_force(path: str) -> None:
    """Best-effort recursive delete that clears read-only bits (Windows keeps git's
    .git/objects read-only, which makes a plain rmtree fail). Never raises."""
    for attempt in (1, 2):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == 2:
                return  # give up gracefully; the OS temp cleaner will reclaim it
            for root, dirs, files in os.walk(path):
                for name in dirs + files:
                    try:
                        os.chmod(os.path.join(root, name), 0o700)
                    except OSError:
                        pass


def _color_enabled(stream: Any = None) -> bool:
    if _NO_COLOR:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    s = stream if stream is not None else sys.stdout
    isatty = getattr(s, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


def _c(text: str, code: str, stream: Any = None) -> str:
    if not _color_enabled(stream):
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(s: str) -> str:
    return _c(s, "32")


def _yellow(s: str) -> str:
    return _c(s, "33")


def _red(s: str) -> str:
    return _c(s, "31", sys.stderr)


def _cyan(s: str) -> str:
    return _c(s, "36")


def _dim(s: str) -> str:
    return _c(s, "2")


def _bold(s: str) -> str:
    return _c(s, "1")


def _eprint(*parts: str) -> None:
    print(*parts, file=sys.stderr)


def _out(*parts: str) -> None:
    if _QUIET:
        return
    print(*parts)


def _dump_json(obj: Any) -> str:
    """Stable, human-readable JSON with a trailing newline for clean diffs."""
    return json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def _emit_json(obj: Any) -> None:
    sys.stdout.write(_dump_json(obj))


def _why(args: argparse.Namespace, header: str, *lines: str) -> None:
    """Structured explanation on stderr, only when --why is set."""
    if not getattr(args, "why", False):
        return
    _eprint(f"[why] {header}")
    for line in lines:
        _eprint(f"  {line}")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 80) -> str:
    """Filesystem- and url-safe slug, capped to a path-component-safe length.

    Falls back to 'untitled' for empty input. The cap keeps directory names well under
    the 255-byte filesystem limit so long contract titles cannot crash ingest.
    """
    out = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    if len(out) > max_len:
        out = out[:max_len].rstrip("-")
    return out or "untitled"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ics_stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
_MONTHS.update({m[:3].lower(): i for m, i in list(_MONTHS.items())})
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_NAME_RE = re.compile(
    r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"
)
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b")


def parse_date(value: Any) -> Optional[dt.date]:
    """Deterministically parse a date from common contract formats.

    Accepts ISO 8601 (date or datetime, optional 'Z'/offset), 'YYYY/MM/DD',
    'Month D, YYYY', 'Mon D, YYYY' and 'D Month YYYY'. Returns None if nothing
    parseable is found. Intentionally avoids ambiguous numeric forms like MM/DD/YYYY.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # ISO date or datetime: take the leading date component.
    iso = _ISO_DATE_RE.search(text)
    if iso and text[: iso.start()].strip() == "":
        try:
            return dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{4})/(\d{2})/(\d{2})$", text)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    name = _MONTH_NAME_RE.search(text)
    if name:
        mon = _MONTHS.get(name.group(1).lower())
        if mon:
            try:
                return dt.date(int(name.group(3)), mon, int(name.group(2)))
            except ValueError:
                return None
    dm = _DAY_MONTH_RE.search(text)
    if dm:
        mon = _MONTHS.get(dm.group(2).lower())
        if mon:
            try:
                return dt.date(int(dm.group(3)), mon, int(dm.group(1)))
            except ValueError:
                return None
    return None


# Stricter ISO matcher for scanning free text: rejects dates embedded in a larger token
# (e.g. the "2026-07-15" inside "invoice-2026-07-15-A") by forbidding adjacent digits / - / /.
_SCAN_ISO_RE = re.compile(r"(?<![\d/-])(\d{4})-(\d{2})-(\d{2})(?![\d/-])")


def scan_dates(text: str) -> List[dt.date]:
    """All recognizable dates in free text, in order of appearance, de-duplicated.

    Turns obligation prose into deadlines; a clause naming two dates yields two.
    """
    found: List[Tuple[int, dt.date]] = []

    def add(pos: int, y: int, mo: int, d: int) -> None:
        try:
            found.append((pos, dt.date(y, mo, d)))
        except ValueError:
            pass

    for m in _SCAN_ISO_RE.finditer(text):
        add(m.start(), int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in _MONTH_NAME_RE.finditer(text):
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            add(m.start(), int(m.group(3)), mon, int(m.group(2)))
    for m in _DAY_MONTH_RE.finditer(text):
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            add(m.start(), int(m.group(3)), mon, int(m.group(1)))
    out: List[dt.date] = []
    for _pos, d in sorted(found, key=lambda t: t[0]):
        if d not in out:
            out.append(d)
    return out


def scan_date(text: str) -> Optional[dt.date]:
    """First recognizable date in free text (None if none)."""
    ds = scan_dates(text)
    return ds[0] if ds else None


_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR"}
_MONEY_NUM_RE = re.compile(r"(\d[\d.,]*)\s*([kKmMbB][nN]?)?")


def _normalize_number(s: str) -> str:
    """Normalize a US- or European-grouped numeric string to a float-parseable form.

    The decimal separator is inferred from context: when both ',' and '.' appear, the
    rightmost is decimal; a lone ',' or '.' with 1-2 trailing digits is decimal, else a
    thousands separator. So '1,234.56'->'1234.56' and '1.000.000,50'->'1000000.50'.
    """
    s = s.strip().strip(".,")
    has_c, has_d = "," in s, "." in s
    if has_c and has_d:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_c:
        parts = s.split(",")
        s = s.replace(",", ".") if len(parts) == 2 and 1 <= len(parts[1]) <= 2 else s.replace(",", "")
    elif has_d:
        parts = s.split(".")
        if not (len(parts) == 2 and 1 <= len(parts[1]) <= 2):
            s = s.replace(".", "")
    return s


def parse_money(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Deterministically parse an amount + currency from a value or string.

    Handles numbers, '$120,000', 'USD 120000', '120000 USD', and k/m/b suffixes.
    Returns (amount, currency) with either element possibly None.
    """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, dict):
        amt = value.get("amount")
        cur = value.get("currency")
        if isinstance(amt, (int, float)) and not isinstance(amt, bool):
            return float(amt), (str(cur) if cur else None)
        return parse_money(value.get("raw") or value.get("value"))
    if not isinstance(value, str):
        return None, None
    text = value.strip()
    if not text:
        return None, None
    currency: Optional[str] = None
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in text:
            currency = code
            break
    if currency is None:
        for token in re.findall(r"[A-Za-z]{3}", text):
            if token.upper() in _CURRENCY_CODES:
                currency = token.upper()
                break
    m = _MONEY_NUM_RE.search(text)
    if not m:
        return None, currency
    try:
        amount = float(_normalize_number(m.group(1)))
    except ValueError:
        return None, currency
    suffix = (m.group(2) or "").lower()
    if suffix.startswith("k"):
        amount *= 1_000
    elif suffix.startswith("m"):
        amount *= 1_000_000
    elif suffix.startswith("b"):
        amount *= 1_000_000_000
    first_digit = re.search(r"\d", text)
    if (first_digit is not None and "-" in text[: first_digit.start()]) or (
        text.startswith("(") and text.endswith(")")
    ):
        amount = -amount  # leading minus or accounting parentheses -> negative
    return amount, currency


def load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotFoundError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise VaultError(f"invalid JSON in {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    if check and proc.returncode != 0:
        raise VaultError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def _git_available() -> bool:
    return shutil.which("git") is not None


def _vault_commit_identity(repo: Path) -> Tuple[str, str]:
    """Identity for vault commits.

    Honors the user's configured git identity / GIT_AUTHOR_* env when present, and
    otherwise falls back to a neutral 'contract-vault' bot so that ingest and demo
    succeed in CI and fresh environments. NOTE: this is unrelated to the identity used
    for commits to the contract-vault *source* repository.
    """
    name = os.environ.get("GIT_AUTHOR_NAME")
    email = os.environ.get("GIT_AUTHOR_EMAIL")
    if not name:
        got = _git(repo, "config", "user.name", check=False)
        name = got.stdout.strip() or None
    if not email:
        got = _git(repo, "config", "user.email", check=False)
        email = got.stdout.strip() or None
    return name or "contract-vault", email or "contract-vault@localhost"


def git_commit(repo: Path, message: str, paths: Optional[Sequence[Path]] = None) -> str:
    """Stage and commit; returns the new commit hash. Identity-safe in any environment."""
    if paths:
        _git(repo, "add", "--", *[str(p) for p in paths])
    else:
        _git(repo, "add", "-A")
    status = _git(repo, "status", "--porcelain")
    if not status.stdout.strip():
        head = _git(repo, "rev-parse", "HEAD", check=False)
        return head.stdout.strip()
    name, email = _vault_commit_identity(repo)
    _git(
        repo,
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "commit", "-q", "-m", message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# Vault discovery & records
# ---------------------------------------------------------------------------


def is_vault(path: Path) -> bool:
    return (path / VAULT_CONFIG_NAME).is_file()


def load_vault_config(vault: Path) -> JSONObj:
    """Read .contract-vault.json; returns {} if missing/unreadable (never raises)."""
    p = vault / VAULT_CONFIG_NAME
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return cast(JSONObj, data) if isinstance(data, dict) else {}


def save_vault_config(vault: Path, config: JSONObj) -> None:
    _atomic_write_text(vault / VAULT_CONFIG_NAME, _dump_json(config), mode=0o600)


def resolve_vault(args: argparse.Namespace) -> Path:
    """Locate the vault: --vault flag, CONTRACT_VAULT_DIR env, then walk up from cwd."""
    explicit = getattr(args, "vault", None) or os.environ.get("CONTRACT_VAULT_DIR")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not is_vault(path):
            raise NotFoundError(f"not a contract-vault (no {VAULT_CONFIG_NAME}): {path}")
        return path
    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if is_vault(candidate):
            return candidate
    raise NotFoundError(
        "no contract-vault found here or in any parent directory; run "
        "`contract-vault init` first (or pass --vault)"
    )


def load_record(record_dir: Path) -> JSONObj:
    data = load_json_file(record_dir / RECORD_FILENAME)
    if not isinstance(data, dict):
        raise VaultError(f"malformed record (not an object): {record_dir / RECORD_FILENAME}")
    return cast(JSONObj, data)


def iter_record_dirs(vault: Path) -> List[Path]:
    """All deal directories (those containing a record.json), sorted by id."""
    found = [p.parent for p in vault.rglob(RECORD_FILENAME) if ".git" not in p.parts]
    return sorted(found, key=lambda d: deal_id(vault, d))


def deal_id(vault: Path, record_dir: Path) -> str:
    return record_dir.relative_to(vault).as_posix()


def load_all_records(vault: Path) -> List[Tuple[str, Path, JSONObj]]:
    out: List[Tuple[str, Path, JSONObj]] = []
    for d in iter_record_dirs(vault):
        out.append((deal_id(vault, d), d, load_record(d)))
    return out


def find_deal(vault: Path, ident: str) -> Tuple[str, Path, JSONObj]:
    """Resolve a deal by exact id, then by unique leaf name, then by unique prefix."""
    records = load_all_records(vault)
    by_id = {rid: (rid, d, rec) for rid, d, rec in records}
    if ident in by_id:
        return by_id[ident]
    leaf_matches = [t for t in records if t[0].split("/")[-1] == ident]
    if len(leaf_matches) == 1:
        return leaf_matches[0]
    prefix_matches = [t for t in records if t[0].startswith(ident)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if not records:
        raise NotFoundError("vault is empty; nothing to get")
    if len(leaf_matches) > 1 or len(prefix_matches) > 1:
        raise UsageError(f"ambiguous deal id {ident!r}; matches multiple deals")
    raise NotFoundError(f"no deal matching {ident!r}")


# ---------------------------------------------------------------------------
# extract -> record mapping
# ---------------------------------------------------------------------------


def _field(node: Any) -> Tuple[Any, float, str]:
    """Unpack an extract field {value, confidence, source} with safe defaults."""
    if isinstance(node, dict):
        value = node.get("value")
        try:
            conf = float(node.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        source = str(node.get("source", SOURCE_NONE) or SOURCE_NONE)
        return value, conf, source
    return None, 0.0, SOURCE_NONE


def _combine_source(*sources: str) -> str:
    """Provenance of a derived value: 'llm' if any input is llm, else 'deterministic'."""
    sset = {s for s in sources if s}
    if SOURCE_LLM in sset:
        return SOURCE_LLM
    if sset & {SOURCE_DETERMINISTIC, SOURCE_MANUAL}:
        return SOURCE_DETERMINISTIC
    return SOURCE_NONE


def validate_extract_payload(payload: Any) -> JSONObj:
    """Light structural check that this is extract-cli output (the ingest input contract).

    Full schema conformance is covered by tests against the vendored
    extract-output.schema.json; here we only guard against obviously-wrong input.
    """
    if not isinstance(payload, dict):
        raise VaultError("ingest input is not a JSON object (expected extract-cli output)")
    missing = [k for k in ("document", "parties", "dates", "term", "_meta") if k not in payload]
    if missing:
        raise VaultError(
            "ingest input does not look like extract-cli output; missing keys: "
            + ", ".join(missing)
            + "  (pipe `extract <file> --json` into `contract-vault ingest -`)"
        )
    doc = payload.get("document")
    if not isinstance(doc, dict) or "sha256" not in doc:
        raise VaultError("extract output 'document' block is missing or has no sha256")
    return cast(JSONObj, payload)


OBLIGATION_STATUSES = ("open", "done", "waived")


def obligation_id(ob: JSONObj) -> str:
    """Stable, content-derived id for an obligation (referenced by the `obligation` command)."""
    seed = f"{ob.get('type', '')}|{ob.get('description', '')}"
    return "o" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def finalize_obligations(obligations: List[JSONObj], carry: Optional[Dict[str, JSONObj]] = None) -> List[JSONObj]:
    """Stamp a stable id + lifecycle fields (status/owner) on each obligation.

    Preserves any existing status/owner; for freshly-rebuilt derived items, carries the
    prior status/owner forward by id (so a recomputed schedule keeps 'done'/'waived' when
    the obligation is unchanged). Missing status defaults to 'open'.
    """
    carry = carry or {}
    for ob in obligations:
        oid = obligation_id(ob)
        ob["id"] = oid
        prior = carry.get(oid, {})
        if not ob.get("status"):
            ob["status"] = str(prior.get("status") or "open")
        if "owner" not in ob or ob.get("owner") is None:
            ob["owner"] = prior.get("owner")
    return obligations


# Recurrence ----------------------------------------------------------------

RECURRENCE_FREQS = ("weekly", "monthly", "quarterly", "semiannual", "annual")
_RECURRENCE_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}
_RECURRENCE_PATTERNS = [
    ("weekly", r"\bweekly\b|\bevery week\b|\beach week\b"),
    ("quarterly", r"\bquarterly\b|\bevery quarter\b|\beach quarter\b"),
    ("semiannual", r"\bsemi-?annual\w*\b|\bbi-?annual\w*\b|\bevery six months\b|\btwice a year\b"),
    ("annual", r"\bannual\w*\b|\byearly\b|\bper annum\b|\bper year\b|\beach year\b|\bevery year\b"),
    ("monthly", r"\bmonthly\b|\bevery month\b|\beach month\b|\bper month\b"),
]


def _add_days_clamped(d: dt.date, days: int) -> dt.date:
    """`d + timedelta(days=days)`, saturating at dt.date.min/max instead of overflowing.
    A far-future --as-of plus even a capped horizon can exceed dt.date.max (year 9999)."""
    try:
        return d + dt.timedelta(days=days)
    except OverflowError:
        return dt.date.max if days >= 0 else dt.date.min


def _add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    year = d.year + m // 12
    month = m % 12 + 1
    # Saturate at dt.date.max/min: a far-future anchor stepped forward can exceed
    # year 9999, which dt.date(...) rejects with ValueError. Clamping keeps
    # recurrence expansion from crashing near the date bounds.
    if year > dt.MAXYEAR:
        return dt.date.max
    if year < dt.MINYEAR:
        return dt.date.min
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(d.day, last))


def _step_recurrence(d: dt.date, freq: str) -> dt.date:
    if freq == "weekly":
        return d + dt.timedelta(days=7)
    return _add_months(d, _RECURRENCE_MONTHS[freq])


def _nth_occurrence(anchor: dt.date, freq: str, k: int) -> dt.date:
    """The k-th occurrence (k >= 0) of a recurrence, computed from the ORIGINAL
    anchor so month-end clamping never accumulates. A 31st anchor stepped monthly
    yields Jan 31 -> Feb 28/29 -> Mar 31 -> Apr 30 instead of getting stuck at 28.
    """
    if freq == "weekly":
        return anchor + dt.timedelta(days=7 * k)
    return _add_months(anchor, _RECURRENCE_MONTHS[freq] * k)


def detect_recurrence(text: str) -> Optional[str]:
    """Conservatively detect a recurrence frequency word in obligation prose."""
    low = text.lower()
    for freq, rx in _RECURRENCE_PATTERNS:
        if re.search(rx, low):
            return freq
    return None


def recurrence_occurrences(anchor: dt.date, freq: str, start: dt.date, end: dt.date, *, cap: int = 400) -> List[dt.date]:
    """Dates of a recurring obligation within [start, end], stepping from the anchor."""
    if freq not in RECURRENCE_FREQS or end < start:
        return []
    # Skip to the first occurrence on/after start, computing each from the original
    # anchor so month-end clamping never compounds (Jan 31 -> Feb 28 -> Mar 31).
    k = 0
    d = _nth_occurrence(anchor, freq, k)
    while d < start and k < 20000:
        k += 1
        d = _nth_occurrence(anchor, freq, k)
    out: List[dt.date] = []
    while d <= end and len(out) < cap:
        out.append(d)
        k += 1
        d = _nth_occurrence(anchor, freq, k)
    return out


def _coerce_leads(value: Any) -> List[int]:
    if not isinstance(value, list):
        return []
    # Drop negative and absurdly large lead-days: a hand-edited reminder above ~100
    # years would overflow `as_of + timedelta(days=max(reminders))` in projection.
    return sorted(
        {
            int(x)
            for x in value
            if isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= MAX_HORIZON_DAYS
        },
        reverse=True,
    )


def obligation_reminders(ob: JSONObj, defaults: Optional[JSONObj] = None) -> List[int]:
    """Lead-day reminders for an obligation, resolved in order:
    explicit per-obligation override -> vault default for this type -> vault 'default'
    catch-all -> built-in type default.
    """
    explicit = _coerce_leads(ob.get("reminders"))
    if explicit:
        return explicit
    typ = str(ob.get("type", ""))
    for key in (typ, "default"):
        leads = _coerce_leads((defaults or {}).get(key))
        if leads:
            return leads
    return [_suggest_lead_days(typ)]


def build_record(
    payload: JSONObj,
    *,
    deal_identifier: str,
    title: Optional[str],
    source_rel_path: str,
    source_vaulted: bool,
) -> JSONObj:
    """Map an extract-cli payload onto a contract-vault record. Pure / deterministic."""
    field_meta: JSONObj = {}

    def record_field(name: str, conf: float, source: str) -> None:
        field_meta[name] = {"confidence": round(conf, 4), "source": source}

    # Parties ---------------------------------------------------------------
    parties: List[JSONObj] = []
    for p in payload.get("parties", []) or []:
        if not isinstance(p, dict):
            continue
        try:
            conf = float(p.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        parties.append(
            {
                "name": str(p.get("name", "")),
                "role": p.get("role"),
                "source": str(p.get("source", SOURCE_NONE) or SOURCE_NONE),
                "confidence": round(conf, 4),
            }
        )

    # Dates -----------------------------------------------------------------
    dates = payload.get("dates", {}) or {}
    eff_val, eff_conf, eff_src = _field(dates.get("effective"))
    exp_val, exp_conf, exp_src = _field(dates.get("expiration"))
    effective = parse_date(eff_val)
    expiration = parse_date(exp_val)
    record_field("effective_date", eff_conf, eff_src)
    record_field("expiration_date", exp_conf, exp_src)

    # Term ------------------------------------------------------------------
    term = payload.get("term", {}) or {}
    len_val, len_conf, len_src = _field(term.get("length"))
    ar_val, ar_conf, ar_src = _field(term.get("auto_renew"))
    np_val, np_conf, np_src = _field(term.get("notice_period_days"))
    auto_renew = ar_val if isinstance(ar_val, bool) else None
    notice_days: Optional[int] = None
    if isinstance(np_val, bool):
        notice_days = None
    elif isinstance(np_val, (int, float)):
        notice_days = int(np_val)
    elif isinstance(np_val, str):
        nm = re.search(r"\d+", np_val)
        notice_days = int(nm.group()) if nm else None
    # Clamp a crafted/absurd notice period out of range: a value above ~100 years
    # would overflow `expiration - timedelta(days=notice_days)` below. Drop it
    # (no renewal window) rather than crash on malformed input.
    if notice_days is not None and not (0 <= notice_days <= MAX_HORIZON_DAYS):
        notice_days = None
    record_field("term.length", len_conf, len_src)
    record_field("term.auto_renew", ar_conf, ar_src)
    record_field("term.notice_period_days", np_conf, np_src)

    renewal_window: Optional[JSONObj] = None
    if expiration is not None and notice_days is not None:
        deadline = expiration - dt.timedelta(days=notice_days)
        renewal_window = {
            "deadline": deadline.isoformat(),
            "expiration": expiration.isoformat(),
        }

    # Governing law & value -------------------------------------------------
    gl_val, gl_conf, gl_src = _field(payload.get("governing_law"))
    governing_law = gl_val if isinstance(gl_val, str) and gl_val.strip() else None
    record_field("governing_law", gl_conf, gl_src)

    val_val, val_conf, val_src = _field(payload.get("value"))
    amount, currency = parse_money(val_val)
    value_obj: JSONObj = {"raw": val_val, "amount": amount, "currency": currency}
    record_field("value", val_conf, val_src)

    # Obligations (computed, deterministic; the engine behind `due`) ---------
    obligations: List[JSONObj] = []
    if expiration is not None:
        obligations.append(
            {
                "type": "expiration",
                "due": expiration.isoformat(),
                "description": "Contract expires.",
                "source": exp_src if exp_src != SOURCE_NONE else SOURCE_DETERMINISTIC,
                "confidence": round(exp_conf, 4),
            }
        )
    if renewal_window is not None and notice_days is not None and expiration is not None:
        ar_note = "auto-renews" if auto_renew else ("does not auto-renew" if auto_renew is False else "renewal terms apply")
        obligations.append(
            {
                "type": "renewal_notice",
                "due": renewal_window["deadline"],
                "description": (
                    f"Notice deadline: {notice_days} days before expiration "
                    f"({expiration.isoformat()}); contract {ar_note}."
                ),
                "source": _combine_source(exp_src, np_src, ar_src),
                "confidence": round(min(exp_conf, np_conf), 4),
            }
        )
    for ob in payload.get("obligations", []) or []:
        if not isinstance(ob, dict):
            continue
        text = str(ob.get("text", "")).strip()
        if not text:
            continue
        try:
            oconf = float(ob.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            oconf = 0.0
        ob_src = str(ob.get("source", SOURCE_NONE) or SOURCE_NONE)
        ob_dues: List[Optional[dt.date]] = [d for d in scan_dates(text)]
        if not ob_dues:
            ob_dues = [None]  # dateless obligation: still recorded, just no deadline
        rec_freq = detect_recurrence(text)
        for due in ob_dues:
            entry: JSONObj = {
                "type": "obligation",
                "due": due.isoformat() if due else None,
                "description": text,
                "source": ob_src,
                "confidence": round(oconf, 4),
            }
            if rec_freq and due is not None:  # only a dated obligation can recur (needs an anchor)
                entry["recurrence"] = rec_freq
            obligations.append(entry)
    finalize_obligations(obligations)  # stamp stable id + status=open + owner=null

    # Provenance ------------------------------------------------------------
    meta = payload.get("_meta", {}) or {}
    doc = payload.get("document", {}) or {}
    provenance: JSONObj = {
        "from_extract": True,
        "extractor_version": str(meta.get("extractor_version", "unknown")),
        "llm_used": bool(meta.get("llm_used", False)),
        "ingested_at": _now_iso(),
    }

    record: JSONObj = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "id": deal_identifier,
        "title": title,
        "status": "signed",
        "signed_on": effective.isoformat() if effective else None,
        "parties": parties,
        "effective_date": effective.isoformat() if effective else None,
        "expiration_date": expiration.isoformat() if expiration else None,
        "term": {
            "length": len_val if isinstance(len_val, str) else (str(len_val) if len_val is not None else None),
            "auto_renew": auto_renew,
            "notice_period_days": notice_days,
            "renewal_window": renewal_window,
        },
        "governing_law": governing_law,
        "value": value_obj,
        "source": {
            "path": source_rel_path,
            "sha256": str(doc.get("sha256", "")),
            "format": str(doc.get("format", "unknown")),
            "vaulted": source_vaulted,
        },
        "obligations": obligations,
        "provenance": provenance,
        "field_meta": field_meta,
    }
    return record


# --- Malformed-record coercion -------------------------------------------------
# record.json is user- and git-editable, so any nested field may be null, a string,
# a list, etc. These helpers normalise the shapes the reader expects so a hand-edited
# record yields a clean error/empty result instead of an AttributeError traceback.


def _as_dict(value: Any) -> JSONObj:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> List[JSONObj]:
    """List elements that are dicts (skips nulls/strings/etc.)."""
    return [x for x in _as_list(value) if isinstance(x, dict)]


def _dict_at(record: JSONObj, key: str) -> JSONObj:
    """Return record[key] as a dict, coercing a missing/null/non-dict value to {} in place."""
    sub = record.get(key)
    if not isinstance(sub, dict):
        sub = {}
        record[key] = sub
    return sub


def record_searchable_text(record: JSONObj) -> str:
    parts: List[str] = [str(record.get("title") or ""), str(record.get("governing_law") or "")]
    for p in _dicts(record.get("parties")):
        parts.append(str(p.get("name", "")))
        parts.append(str(p.get("role") or ""))
    for ob in _dicts(record.get("obligations")):
        parts.append(str(ob.get("description", "")))
    val = record.get("value", {})
    if isinstance(val, dict):
        parts.append(str(val.get("raw") or ""))
    return "  ".join(parts).lower()


# ---------------------------------------------------------------------------
# Obligations / due projection (pure, deterministic) -> the `due` view
# ---------------------------------------------------------------------------


def parse_within(text: str) -> int:
    """Parse a window like '90d', '90', '30', '12w', '6m' into a day count."""
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([dwmy])?", t)
    if not m:
        raise UsageError(f"invalid --within value: {text!r} (try 30d, 60d, 90d)")
    n = int(m.group(1))
    unit = m.group(2) or "d"
    days = {"d": n, "w": n * 7, "m": n * 30, "y": n * 365}[unit]
    # Cap at ~100 years: a larger window overflows `as_of + timedelta(days=...)`.
    if days > MAX_HORIZON_DAYS:
        raise UsageError(f"--within window too large: {text!r} (max ~100 years)")
    return days


def upcoming_obligations(
    vault: Path,
    *,
    within_days: int,
    as_of: dt.date,
    statuses: Optional[Sequence[str]] = None,
    type_filter: Optional[str] = None,
    owner_filter: Optional[str] = None,
    remind_only: bool = False,
) -> List[JSONObj]:
    """Project dated obligations across all deals into actions within the window.

    By default only OPEN obligations are projected (so completing one drops it from the
    calendar); pass statuses=("open","done","waived") or None-with-all to widen. Missing
    status is treated as 'open' for backward compatibility with pre-0.2.0 records.

    With remind_only, `within_days` is ignored and each obligation is included only when a
    reminder is currently active -- i.e. `0 <= days_until <= max(its reminders)`.
    """
    include = set(statuses) if statuses else {"open"}
    horizon = _add_days_clamped(as_of, within_days)
    reminder_defaults = load_vault_config(vault).get("reminder_defaults")
    reminder_defaults = reminder_defaults if isinstance(reminder_defaults, dict) else {}
    rows: List[JSONObj] = []
    for rid, _dir, rec in load_all_records(vault):
        counterparty = primary_counterparty(rec)
        exp_cap = parse_date(rec.get("expiration_date"))
        for ob in _dicts(rec.get("obligations")):
            if str(ob.get("status") or "open") not in include:
                continue
            if type_filter and str(ob.get("type", "")) != type_filter:
                continue
            if owner_filter and str(ob.get("owner") or "").lower() != owner_filter.lower():
                continue
            anchor = parse_date(ob.get("due"))
            if anchor is None:
                continue  # dateless obligation: tracked, but no calendar entry
            reminders = obligation_reminders(ob, reminder_defaults)
            # remind_only: each obligation's own reminder window; else the shared horizon.
            end = _add_days_clamped(as_of, max(reminders)) if remind_only else horizon
            freq = ob.get("recurrence") if ob.get("recurrence") in RECURRENCE_FREQS else None
            if freq:
                # Recurring: expand into occurrences, capped at the contract's expiration.
                cap_end = min(end, exp_cap) if exp_cap else end
                occurrences = recurrence_occurrences(anchor, str(freq), as_of, cap_end)
            else:
                occurrences = [anchor] if as_of <= anchor <= end else []
            for due in occurrences:
                rows.append(
                    {
                        "id": ob.get("id", obligation_id(ob)),
                        "deal": rid,
                        "counterparty": counterparty,
                        "type": ob.get("type", "obligation"),
                        "due": due.isoformat(),
                        "days_until": (due - as_of).days,
                        "description": ob.get("description", ""),
                        "status": str(ob.get("status") or "open"),
                        "owner": ob.get("owner"),
                        "recurrence": freq,
                        "reminders": reminders,
                        "source": ob.get("source", SOURCE_NONE),
                        "confidence": ob.get("confidence", 0.0),
                        "lead_days": max(reminders),
                    }
                )
    rows.sort(key=lambda r: (r["due"], r["deal"]))
    return rows


def _suggest_lead_days(ob_type: str) -> int:
    return {"renewal_notice": 14, "expiration": 30, "obligation": 7}.get(ob_type, 7)


def _obligation_line(r: JSONObj) -> str:
    """One human-readable line for an upcoming-obligation row (shared by `due`/`remind`)."""
    when = _yellow(str(r["due"])) if r["days_until"] <= 14 else str(r["due"])
    owner = f"  @{r['owner']}" if r.get("owner") else ""
    recur = _dim(f" (every {r['recurrence']})") if r.get("recurrence") else ""
    return f"  {_dim(str(r['id']))}  {when}  (+{r['days_until']}d)  {_bold(str(r['type']))}{recur}  {r['counterparty']}{owner}  -- {r['description']}"


def primary_counterparty(record: JSONObj) -> str:
    parties = _dicts(record.get("parties"))
    if parties:
        return str(parties[0].get("name", "")) or "(unknown)"
    return "(unknown)"


REVIEW_THRESHOLD = 0.6


def review_flags(record: JSONObj, threshold: float = REVIEW_THRESHOLD) -> List[JSONObj]:
    """Deterministic 'verify, not trust' worklist for a record.

    Flags fields that are unidentified (source=none), LLM-derived (source=llm), or
    low-confidence (< threshold). Pure inspection of the provenance contract-vault already
    stores -- it NEVER calls an LLM. To *improve* flagged fields, re-ingest with
    `ingest --llm`, which delegates the LLM work to extract-cli.
    """
    out: List[JSONObj] = []

    def consider(field: str, label: str, source: Any, confidence: Any) -> None:
        src = str(source or SOURCE_NONE)
        conf = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.0
        reasons: List[str] = []
        if src == SOURCE_NONE:
            reasons.append("unidentified")
        elif src == SOURCE_LLM:
            reasons.append("llm-derived")
        if src != SOURCE_NONE and conf < threshold:
            reasons.append(f"low-confidence({round(conf, 2)})")
        if reasons:
            # 'field' is a canonical, accept-compatible path; 'label' is for display only.
            out.append({"field": field, "label": label, "source": src, "confidence": round(conf, 4), "reasons": reasons})

    for name, meta in _as_dict(record.get("field_meta")).items():
        if isinstance(meta, dict):
            consider(name, name, meta.get("source"), meta.get("confidence"))
    for i, party in enumerate(_as_list(record.get("parties"))):
        if isinstance(party, dict):
            consider(f"parties[{i}]", str(party.get("name", "?")), party.get("source"), party.get("confidence"))
    for i, ob in enumerate(_as_list(record.get("obligations"))):
        if isinstance(ob, dict) and ob.get("type") == "obligation":
            consider(f"obligations[{i}]", str(ob.get("description", ""))[:40], ob.get("source"), ob.get("confidence"))
    return out


def provenance_summary(record: JSONObj) -> Dict[str, int]:
    """Count field_meta entries by source -- the basis for the ingest --why breakdown."""
    counts = {SOURCE_DETERMINISTIC: 0, SOURCE_LLM: 0, SOURCE_MANUAL: 0, SOURCE_NONE: 0}
    for meta in _as_dict(record.get("field_meta")).values():
        if isinstance(meta, dict):
            src = str(meta.get("source") or SOURCE_NONE)
            counts[src] = counts.get(src, 0) + 1
    return counts


def recompute_schedule(record: JSONObj) -> None:
    """Recompute term.renewal_window and the derived (expiration/renewal_notice) obligations
    from the record's current dates/term, preserving text-derived 'obligation' items.

    Mirrors build_record's deterministic derivation; used after a manual edit (`accept`) so
    the calendar stays correct. (A test asserts it is a no-op on freshly-ingested records.)
    """
    term = _as_dict(record.get("term"))
    record["term"] = term
    fm = record.get("field_meta", {})
    exp = parse_date(record.get("expiration_date"))
    raw_notice = term.get("notice_period_days")
    notice = raw_notice if isinstance(raw_notice, int) and not isinstance(raw_notice, bool) else None
    # Clamp out-of-range notice periods (see build_record): an absurd value would
    # overflow `exp - timedelta(days=notice)` below. Drop the renewal window instead.
    if notice is not None and not (0 <= notice <= MAX_HORIZON_DAYS):
        notice = None
    auto = term.get("auto_renew") if isinstance(term.get("auto_renew"), bool) else None

    def meta(name: str) -> Tuple[str, float]:
        m = fm.get(name, {}) if isinstance(fm, dict) else {}
        src = str(m.get("source") or SOURCE_NONE) if isinstance(m, dict) else SOURCE_NONE
        try:
            conf = float(m.get("confidence", 0.0) or 0.0) if isinstance(m, dict) else 0.0
        except (TypeError, ValueError):
            conf = 0.0
        return src, conf

    exp_src, exp_conf = meta("expiration_date")
    np_src, np_conf = meta("term.notice_period_days")
    ar_src, _ar_conf = meta("term.auto_renew")

    rw: Optional[JSONObj] = None
    if exp is not None and notice is not None:
        rw = {"deadline": (exp - dt.timedelta(days=notice)).isoformat(), "expiration": exp.isoformat()}
    term["renewal_window"] = rw

    # Preserve lifecycle (status/owner) across recompute, keyed by stable obligation id.
    carry: Dict[str, JSONObj] = {
        str(o["id"]): {"status": o.get("status"), "owner": o.get("owner")}
        for o in _dicts(record.get("obligations"))
        if o.get("id")
    }
    text_obs = [o for o in _dicts(record.get("obligations")) if o.get("type") == "obligation"]
    derived: List[JSONObj] = []
    if exp is not None:
        derived.append(
            {
                "type": "expiration",
                "due": exp.isoformat(),
                "description": "Contract expires.",
                "source": exp_src if exp_src != SOURCE_NONE else SOURCE_DETERMINISTIC,
                "confidence": round(exp_conf, 4),
            }
        )
    if rw is not None and notice is not None and exp is not None:
        ar_note = "auto-renews" if auto else ("does not auto-renew" if auto is False else "renewal terms apply")
        derived.append(
            {
                "type": "renewal_notice",
                "due": rw["deadline"],
                "description": f"Notice deadline: {notice} days before expiration ({exp.isoformat()}); contract {ar_note}.",
                "source": _combine_source(exp_src, np_src, ar_src),
                "confidence": round(min(exp_conf, np_conf), 4),
            }
        )
    record["obligations"] = finalize_obligations(derived + text_obs, carry)


def _parse_bool(raw: str) -> Optional[bool]:
    t = raw.strip().lower()
    if t in ("true", "t", "yes", "y", "1"):
        return True
    if t in ("false", "f", "no", "n", "0"):
        return False
    return None


# ---------------------------------------------------------------------------
# RFC 5545 (.ics) generation -- stdlib only, no dependency
# ---------------------------------------------------------------------------


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_fold(line: str) -> str:
    """Fold a content line to <=75 octets, continuation lines prefixed with one space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    pieces: List[bytes] = []
    i = 0
    first = True
    while i < len(raw):
        take = 75 if first else 74  # continuation reserves one octet for the leading space
        end = min(i + take, len(raw))
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1  # never split a multibyte UTF-8 sequence
        pieces.append(raw[i:end])
        i = end
        first = False
    return "\r\n ".join(p.decode("utf-8") for p in pieces)


def build_ics(rows: List[JSONObj], *, now: Optional[dt.datetime] = None) -> str:
    """Render upcoming obligations as a valid RFC 5545 VCALENDAR (CRLF, folded)."""
    moment = now or _now_utc()
    stamp = _ics_stamp(moment)
    lines: List[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//DrBaher//contract-vault " + __version__ + "//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for row in rows:
        due = parse_date(row["due"])
        if due is None:
            continue
        dtstart = due.strftime("%Y%m%d")
        dtend = (due + dt.timedelta(days=1)).strftime("%Y%m%d")
        summary = f"{row['counterparty']}: {str(row['type']).replace('_', ' ')}"
        desc = f"{row['description']} [deal: {row['deal']}; source: {row['source']}]"
        uid_seed = f"{row['deal']}|{row['type']}|{row['due']}|{row['description']}"
        uid = sha256_bytes(uid_seed.encode("utf-8"))[:24] + "@contract-vault"
        reminders = row.get("reminders") or [int(row.get("lead_days", 7))]
        lines.extend(
            [
                "BEGIN:VEVENT",
                _ics_fold(f"UID:{uid}"),
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{dtstart}",
                f"DTEND;VALUE=DATE:{dtend}",
                _ics_fold(f"SUMMARY:{_ics_escape(summary)}"),
                _ics_fold(f"DESCRIPTION:{_ics_escape(desc)}"),
                "TRANSP:TRANSPARENT",
            ]
        )
        for lead in reminders:  # one VALARM per configured reminder lead-time
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    _ics_fold(f"DESCRIPTION:{_ics_escape('Reminder: ' + summary)}"),
                    f"TRIGGER:-P{int(lead)}D",
                    "END:VALARM",
                ]
            )
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# LLM config lookup (opt-in, delegated to extract -- never on a hot path here)
# ---------------------------------------------------------------------------


def find_llm_config() -> Optional[Path]:
    candidates = [
        Path.home() / ".config" / "contract-ops" / "llm.json",
        Path.cwd() / "config" / "llm.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


# ---------------------------------------------------------------------------
# Acquiring an extract payload for `ingest`
# ---------------------------------------------------------------------------


def run_extract(document: Path, *, use_llm: bool) -> JSONObj:
    """Shell out to `extract <file> --json`. Never reimplements extraction."""
    if shutil.which("extract") is None:
        raise VaultError(
            "`extract` is not on PATH and no JSON was piped in.\n"
            "  Install it:   pip install extract-cli\n"
            "  Or pipe JSON: extract <file> --json | contract-vault ingest -"
        )
    cmd = ["extract", str(document), "--json"]
    if use_llm:
        cmd.append("--llm")
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise VaultError(f"extract failed (exit {proc.returncode}): {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VaultError(f"extract did not emit valid JSON: {exc}") from exc
    return validate_extract_payload(data)


def acquire_payload(args: argparse.Namespace) -> Tuple[JSONObj, Optional[Path]]:
    """Return (extract_payload, local_source_path_or_None) per the integration rules."""
    target = args.file
    if target == "-":
        raw = sys.stdin.read()
        if not raw.strip():
            raise UsageError("no JSON on stdin (use: extract <file> --json | contract-vault ingest -)")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VaultError(f"stdin is not valid JSON: {exc}") from exc
        return validate_extract_payload(data), None
    path = Path(target).expanduser()
    if not path.exists():
        raise NotFoundError(f"file not found: {path}")
    # A .json file that is already extract output is ingested directly (offline path).
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise VaultError(f"{path} is not valid JSON: {exc}") from exc
        if isinstance(data, dict) and "document" in data and "_meta" in data:
            payload = validate_extract_payload(data)
            # SECURITY: do NOT trust document.source_path from a piped/.json payload. A
            # crafted file could point it at an arbitrary local path (e.g. ~/.ssh/id_rsa)
            # and copy it into the vault. Only `ingest <doc>` (a file the user chose) vaults
            # the source; here source_path is kept as metadata only (vaulted=False).
            return payload, None
    # Otherwise treat it as a document and delegate extraction.
    payload = run_extract(path, use_llm=getattr(args, "llm", False))
    return payload, path


def store_record(
    vault: Path,
    payload: JSONObj,
    *,
    counterparty_override: Optional[str],
    name_override: Optional[str],
    local_source: Optional[Path],
) -> Tuple[str, bool]:
    """Build, place, and commit a record. Returns (deal_id, created)."""
    doc = payload.get("document", {}) or {}
    sha = str(doc.get("sha256", ""))

    # Idempotency: if any existing record already has this source sha256, skip.
    if sha:
        for rid, _d, rec in load_all_records(vault):
            if str(_as_dict(rec.get("source")).get("sha256", "")) == sha:
                return rid, False

    parties = payload.get("parties", []) or []
    first_party = parties[0].get("name") if parties and isinstance(parties[0], dict) else None
    counterparty = counterparty_override or (str(first_party) if first_party else "unfiled")
    title = doc.get("title")
    if name_override:
        name = name_override
    elif isinstance(title, str) and title.strip():
        name = title
    else:
        src_path = doc.get("source_path")
        name = Path(str(src_path)).stem if src_path else (sha[:12] if sha else "deal")

    cp_slug = slugify(counterparty)
    name_slug = slugify(name)
    base_rel = f"{cp_slug}/{name_slug}"
    rel = base_rel
    n = 2
    while (vault / rel).exists():
        rel = f"{base_rel}-{n}"
        n += 1
    deal_dir = vault / rel
    deal_dir.mkdir(parents=True, exist_ok=True)
    _harden(deal_dir, 0o700)

    # Vault the source document if we have a readable local copy.
    source_vaulted = False
    source_rel = str(doc.get("source_path") or "")
    fmt = str(doc.get("format", "unknown"))
    if local_source and local_source.exists():
        ext = local_source.suffix or {"markdown": ".md", "text": ".txt", "pdf": ".pdf", "docx": ".docx", "html": ".html"}.get(fmt, "")
        dest = deal_dir / f"source{ext}"
        shutil.copy2(local_source, dest)
        _harden(dest, 0o600)
        source_rel = dest.name
        source_vaulted = True
        actual = sha256_file(dest)
        if sha and actual != sha:
            doc["sha256"] = actual  # trust the bytes we actually stored

    record = build_record(
        payload,
        deal_identifier=rel,
        title=(title if isinstance(title, str) else None),
        source_rel_path=source_rel or "(not vaulted)",
        source_vaulted=source_vaulted,
    )
    record_path = deal_dir / RECORD_FILENAME
    _atomic_write_text(record_path, _dump_json(record), mode=0o600)

    git_commit(vault, f"ingest: {rel}", paths=[deal_dir])
    return rel, True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(getattr(args, "path", None) or ".").expanduser().resolve()
    encrypt = getattr(args, "encrypt", False)
    if encrypt and shutil.which("git-crypt") is None:
        raise UsageError(
            "--encrypt needs git-crypt, which is not on PATH.\n"
            "  Install it (e.g. `brew install git-crypt` or `apt-get install git-crypt`), then re-run.\n"
            "  Vault not created (so it can't look encrypted when it isn't)."
        )
    path.mkdir(parents=True, exist_ok=True)
    if is_vault(path):
        _out(_yellow(f"already a contract-vault: {path}"))
        return EXIT_OK
    if not _git_available():
        raise VaultError("git is required but was not found on PATH")
    if not (path / ".git").exists():
        _git(path, "init", "-q")
    _harden(path, 0o700)  # owner-only vault dir (defense in depth alongside disk encryption)
    if encrypt:
        _enable_encryption(path)
    config = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "kind": VAULT_KIND,
        "created": _now_iso(),
        "tool": f"contract-vault {__version__}",
        "encrypted": bool(encrypt),
    }
    _atomic_write_text(path / VAULT_CONFIG_NAME, _dump_json(config), mode=0o600)
    readme = path / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Executed-contract vault\n\n"
            "Managed by [contract-vault](https://cli.drbaher.com). "
            "Each deal lives under `<counterparty>/<name>/record.json` alongside its source document.\n",
            encoding="utf-8",
        )
    git_commit(path, "init: contract-vault" + (" (git-crypt encrypted)" if encrypt else ""))
    _why(args, "init", f"vault={path}", f"encrypted={bool(encrypt)}")
    if getattr(args, "json", False):
        _emit_json({"vault": str(path), "kind": VAULT_KIND, "schema_version": RECORD_SCHEMA_VERSION, "encrypted": bool(encrypt)})
    else:
        _out(_green(f"Initialized contract-vault at {path}") + (_dim("  [encrypted at rest via git-crypt]") if encrypt else ""))
        if encrypt:
            _out(_dim("  Back up the key: `git-crypt export-key <file>` (without it, encrypted history is unrecoverable)."))
    return EXIT_OK


def _enable_encryption(vault: Path) -> None:
    """Turn on git-crypt encryption-at-rest for record.json + source documents.

    Encrypts what is committed/pushed (the working tree stays plaintext so the CLI works
    normally). Not a substitute for disk encryption + restrictive perms on the working copy.
    """
    _git(vault, "config", "--local", "user.useConfigOnly", "false", check=False)
    proc = subprocess.run(["git-crypt", "init"], cwd=str(vault), text=True, capture_output=True)
    if proc.returncode != 0 and "already" not in (proc.stderr + proc.stdout).lower():
        raise VaultError(f"git-crypt init failed: {proc.stderr.strip() or proc.stdout.strip()}")
    gitattributes = vault / ".gitattributes"
    gitattributes.write_text(
        "# contract-vault: encrypt deal records and source documents at rest (git-crypt).\n"
        "**/record.json filter=git-crypt diff=git-crypt\n"
        "**/source.* filter=git-crypt diff=git-crypt\n"
        ".contract-vault.json !filter !diff\n"
        ".gitattributes !filter !diff\n"
        "README.md !filter !diff\n",
        encoding="utf-8",
    )


def cmd_ingest(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    _why(
        args,
        "ingest",
        f"source={'stdin' if args.file == '-' else args.file}",
        f"extract_on_path={shutil.which('extract') is not None}",
        f"llm={'on' if getattr(args, 'llm', False) else 'off'}",
    )
    if getattr(args, "llm", False) and find_llm_config() is None:
        _why(args, "ingest.llm", "no llm.json found; extract will use its own defaults")
    payload, local_source = acquire_payload(args)
    deal, created = store_record(
        vault,
        payload,
        counterparty_override=getattr(args, "counterparty", None),
        name_override=getattr(args, "name", None),
        local_source=local_source,
    )
    if getattr(args, "why", False):
        try:
            rec = load_record(vault / deal)
            c = provenance_summary(rec)
            nr = len(review_flags(rec))
            _why(
                args,
                "ingest.provenance",
                f"fields deterministic={c[SOURCE_DETERMINISTIC]} llm={c[SOURCE_LLM]} unidentified={c[SOURCE_NONE]}",
                f"llm_used={rec.get('provenance', {}).get('llm_used')}",
                f"needs_review={nr} (see `contract-vault review`; improve with `ingest --llm`)",
            )
        except VaultError:
            pass
    if getattr(args, "json", False):
        _emit_json({"deal": deal, "created": created, "vault": str(vault)})
    elif created:
        _out(_green(f"Ingested {deal}"))
    else:
        _out(_yellow(f"Already ingested (same sha256): {deal}"))
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    records = load_all_records(vault)
    if getattr(args, "json", False):
        _emit_json(
            {
                "vault": str(vault),
                "count": len(records),
                "deals": [
                    {
                        "id": rid,
                        "title": rec.get("title"),
                        "counterparty": primary_counterparty(rec),
                        "expiration_date": rec.get("expiration_date"),
                        "value": _as_dict(rec.get("value")).get("amount"),
                        "currency": _as_dict(rec.get("value")).get("currency"),
                        "status": rec.get("status"),
                    }
                    for rid, _d, rec in records
                ],
            }
        )
        return EXIT_OK
    if not records:
        _out(_dim("(no deals yet -- ingest one with `contract-vault ingest <file>`)"))
        return EXIT_OK
    for rid, _d, rec in records:
        exp = rec.get("expiration_date") or "-"
        cp = primary_counterparty(rec)
        _out(f"{_bold(rid)}  {_dim('exp')} {exp}  {_dim('cp')} {cp}")
    _out(_dim(f"{len(records)} deal(s)"))
    return EXIT_OK


def cmd_get(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    rid, _dir, rec = find_deal(vault, args.id)
    if getattr(args, "json", False):
        _emit_json(rec)
        return EXIT_OK
    _out(_bold(rid))
    _out(f"  title:           {rec.get('title')}")
    _out(f"  status:          {rec.get('status')}  signed_on {rec.get('signed_on')}")
    _out(f"  parties:         " + ", ".join(f"{p.get('name')} ({p.get('role') or 'party'})" for p in _dicts(rec.get("parties"))))
    _out(f"  effective:       {rec.get('effective_date')}")
    _out(f"  expiration:      {rec.get('expiration_date')}")
    term = _as_dict(rec.get("term"))
    _out(f"  term:            length={term.get('length')} auto_renew={term.get('auto_renew')} notice_days={term.get('notice_period_days')}")
    rw = _as_dict(term.get("renewal_window"))
    if rw:
        _out(f"  renewal window:  notice by {rw.get('deadline')} (expires {rw.get('expiration')})")
    _out(f"  governing law:   {rec.get('governing_law')}")
    val = _as_dict(rec.get("value"))
    _out(f"  value:           {val.get('raw')} (amount={val.get('amount')} {val.get('currency') or ''})")
    src = _as_dict(rec.get("source"))
    _out(f"  source:          {src.get('path')} [{src.get('format')}] sha256={str(src.get('sha256'))[:12]} vaulted={src.get('vaulted')}")
    obs = _dicts(rec.get("obligations"))
    if obs:
        _out("  obligations:")
        for ob in obs:
            owner = f" @{ob.get('owner')}" if ob.get("owner") else ""
            recur = f" every {ob.get('recurrence')}" if ob.get("recurrence") else ""
            rem = f" reminders={ob.get('reminders')}" if ob.get("reminders") else ""
            st = str(ob.get("status") or "open")
            _out(f"    - {_dim(str(ob.get('id', '')))} [{ob.get('type')}] {st}{owner}{recur}{rem} due {ob.get('due') or '-'}  {ob.get('description')}  ({ob.get('source')}, conf {ob.get('confidence')})")
    prov = _as_dict(rec.get("provenance"))
    _out(_dim(f"  provenance:      from_extract={prov.get('from_extract')} extractor={prov.get('extractor_version')} llm_used={prov.get('llm_used')} ingested {prov.get('ingested_at')}"))
    flags = review_flags(rec)
    if flags:
        _out(_yellow(f"  needs review:    {len(flags)} field(s): " + ", ".join(str(f["field"]) for f in flags)))
    return EXIT_OK


def cmd_find(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    records = load_all_records(vault)
    exp_before = parse_date(args.expiring_before) if getattr(args, "expiring_before", None) else None
    if getattr(args, "expiring_before", None) and exp_before is None:
        raise UsageError(f"invalid --expiring-before date: {args.expiring_before!r}")
    results: List[Tuple[str, Path, JSONObj]] = []
    for rid, d, rec in records:
        if args.counterparty:
            if args.counterparty.lower() not in " ".join(str(p.get("name", "")) for p in _dicts(rec.get("parties"))).lower():
                continue
        if args.governing_law:
            if args.governing_law.lower() not in str(rec.get("governing_law") or "").lower():
                continue
        if exp_before is not None:
            exp = parse_date(rec.get("expiration_date"))
            if exp is None or exp >= exp_before:
                continue
        if getattr(args, "currency", None):
            want = args.currency.strip().lower()
            cur = _as_dict(rec.get("value")).get("currency")
            if want in ("none", "unknown", "null", "-"):
                if cur is not None:
                    continue
            elif str(cur or "").lower() != want:
                continue
        if getattr(args, "value_gt", None) is not None:
            amount = _as_dict(rec.get("value")).get("amount")
            if not isinstance(amount, (int, float)) or amount <= args.value_gt:
                continue
        if getattr(args, "auto_renew", False):
            if _as_dict(rec.get("term")).get("auto_renew") is not True:
                continue
        if args.text:
            if args.text.lower() not in record_searchable_text(rec):
                continue
        if getattr(args, "needs_review", False):
            if not review_flags(rec):
                continue
        results.append((rid, d, rec))
    _why(
        args,
        "find",
        f"scanned={len(records)} matched={len(results)}",
        "filters="
        + " ".join(
            f"{k}={v}"
            for k, v in (
                ("counterparty", args.counterparty),
                ("governing_law", args.governing_law),
                ("currency", getattr(args, "currency", None)),
                ("expiring_before", getattr(args, "expiring_before", None)),
                ("value_gt", getattr(args, "value_gt", None)),
                ("auto_renew", getattr(args, "auto_renew", False)),
                ("needs_review", getattr(args, "needs_review", False)),
                ("text", args.text),
            )
            if v
        )
        or "(none)",
    )
    if getattr(args, "json", False):
        _emit_json(
            {
                "count": len(results),
                "deals": [
                    {
                        "id": rid,
                        "title": rec.get("title"),
                        "counterparty": primary_counterparty(rec),
                        "expiration_date": rec.get("expiration_date"),
                        "governing_law": rec.get("governing_law"),
                        "value": _as_dict(rec.get("value")).get("amount"),
                        "currency": _as_dict(rec.get("value")).get("currency"),
                        "auto_renew": _as_dict(rec.get("term")).get("auto_renew"),
                    }
                    for rid, _d, rec in results
                ],
            }
        )
        return EXIT_OK
    if not results:
        _out(_dim("(no matches)"))
        return EXIT_OK
    for rid, _d, rec in results:
        val = _as_dict(rec.get("value"))
        amt = val.get("amount")
        money = (
            f"{amt:,.0f} {val.get('currency') or '?'}"
            if isinstance(amt, (int, float)) and not isinstance(amt, bool)
            else "-"
        )
        _out(f"{_bold(rid)}  exp {rec.get('expiration_date') or '-'}  val {money}  law {rec.get('governing_law') or '-'}")
    return EXIT_OK


def cmd_due(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    within = parse_within(args.within) if getattr(args, "within", None) else DEFAULT_WITHIN_DAYS
    as_of = parse_date(args.as_of) if getattr(args, "as_of", None) else dt.date.today()
    if getattr(args, "as_of", None) and as_of is None:
        raise UsageError(f"invalid --as-of date: {args.as_of!r}")
    assert as_of is not None
    status_arg = getattr(args, "status", None) or "open"
    statuses = list(OBLIGATION_STATUSES) if status_arg == "all" else [status_arg]
    rows = upcoming_obligations(
        vault, within_days=within, as_of=as_of, statuses=statuses,
        type_filter=getattr(args, "type", None), owner_filter=getattr(args, "owner", None),
    )
    fmt = getattr(args, "format", None) or ("json" if getattr(args, "json", False) else "table")
    if getattr(args, "json", False):
        fmt = "json"
    _why(args, "due", f"as_of={as_of.isoformat()}", f"within_days={within}", f"status={status_arg}", f"actions={len(rows)}", f"format={fmt}")
    if fmt == "ics":
        sys.stdout.write(build_ics(rows))
        return EXIT_OK
    if fmt == "json":
        _emit_json(
            {
                "generated_at": _now_iso(),
                "as_of": as_of.isoformat(),
                "within_days": within,
                "count": len(rows),
                "obligations": rows,
            }
        )
        return EXIT_OK
    # table
    if not rows:
        _out(_dim(f"(no obligations due within {within} days as of {as_of.isoformat()})"))
        return EXIT_OK
    _out(_bold(f"Upcoming obligations within {within} days (as of {as_of.isoformat()}):"))
    for r in rows:
        _out(_obligation_line(r))
    return EXIT_OK


def cmd_remind(args: argparse.Namespace) -> int:
    """Obligations whose reminder window is open right now (0 <= days_until <= its longest
    lead). The reminder digest for cron/agents: `contract-vault remind --strict --json`
    daily emits exactly what to notify about. Deterministic; never calls an LLM."""
    vault = resolve_vault(args)
    as_of = parse_date(args.as_of) if getattr(args, "as_of", None) else dt.date.today()
    if getattr(args, "as_of", None) and as_of is None:
        raise UsageError(f"invalid --as-of date: {args.as_of!r}")
    assert as_of is not None
    status_arg = getattr(args, "status", None) or "open"
    statuses = list(OBLIGATION_STATUSES) if status_arg == "all" else [status_arg]
    rows = upcoming_obligations(
        vault, within_days=0, as_of=as_of, statuses=statuses,
        type_filter=getattr(args, "type", None), owner_filter=getattr(args, "owner", None),
        remind_only=True,
    )
    rc = EXIT_FAIL if (rows and getattr(args, "strict", False)) else EXIT_OK
    fmt = getattr(args, "format", None) or ("json" if getattr(args, "json", False) else "table")
    if getattr(args, "json", False):
        fmt = "json"
    _why(args, "remind", f"as_of={as_of.isoformat()}", f"due_now={len(rows)}", f"strict={getattr(args, 'strict', False)}", f"format={fmt}")
    if fmt == "ics":
        sys.stdout.write(build_ics(rows))
        return rc
    if fmt == "json":
        _emit_json({"generated_at": _now_iso(), "as_of": as_of.isoformat(), "count": len(rows), "obligations": rows})
        return rc
    if not rows:
        _out(_green(f"No reminders due as of {as_of.isoformat()}."))
        return EXIT_OK
    _out(_bold(f"Reminders due as of {as_of.isoformat()} ({len(rows)}):"))
    for r in rows:
        _out(_obligation_line(r))
    return rc


def cmd_stats(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    records = load_all_records(vault)
    today = parse_date(args.as_of) if getattr(args, "as_of", None) else dt.date.today()
    if getattr(args, "as_of", None) and today is None:
        raise UsageError(f"invalid --as-of date: {args.as_of!r}")
    assert today is not None
    soon = today + dt.timedelta(days=DEFAULT_WITHIN_DAYS)
    by_counterparty: Dict[str, int] = {}
    by_law: Dict[str, int] = {}
    totals: Dict[str, float] = {}
    expiring_soon = 0
    auto_renew = 0
    llm_used = 0
    needs_review = 0
    for _rid, _d, rec in records:
        cp = primary_counterparty(rec)
        by_counterparty[cp] = by_counterparty.get(cp, 0) + 1
        law = rec.get("governing_law") or "(unspecified)"
        by_law[law] = by_law.get(law, 0) + 1
        val = _as_dict(rec.get("value"))
        amount = val.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            cur = val.get("currency") or "(unknown)"
            totals[cur] = totals.get(cur, 0.0) + float(amount)
        exp = parse_date(rec.get("expiration_date"))
        if exp is not None and today <= exp <= soon:
            expiring_soon += 1
        if _as_dict(rec.get("term")).get("auto_renew") is True:
            auto_renew += 1
        if _as_dict(rec.get("provenance")).get("llm_used") is True:
            llm_used += 1
        if review_flags(rec):
            needs_review += 1
    payload = {
        "vault": str(vault),
        "count": len(records),
        "total_value": {k: round(v, 2) for k, v in sorted(totals.items())},
        "expiring_within_90d": expiring_soon,
        "auto_renew_count": auto_renew,
        "llm_used_count": llm_used,
        "needs_review_count": needs_review,
        "by_counterparty": dict(sorted(by_counterparty.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_governing_law": dict(sorted(by_law.items(), key=lambda kv: (-kv[1], kv[0]))),
    }
    if getattr(args, "json", False):
        _emit_json(payload)
        return EXIT_OK
    _out(_bold(f"Portfolio: {payload['count']} deal(s)"))
    if totals:
        _out("  total value: " + ", ".join(f"{round(v, 2)} {k}" for k, v in sorted(totals.items())))
    _out(f"  expiring within 90d: {expiring_soon}    auto-renewing: {auto_renew}")
    _out(f"  llm-assisted (extract): {llm_used}    needs review: {needs_review}  (`contract-vault review`)")
    if by_counterparty:
        _out("  by counterparty:")
        for cp, n in sorted(by_counterparty.items(), key=lambda kv: (-kv[1], kv[0])):
            _out(f"    {n:>3}  {cp}")
    if by_law:
        _out("  by governing law:")
        for law, n in sorted(by_law.items(), key=lambda kv: (-kv[1], kv[0])):
            _out(f"    {n:>3}  {law}")
    return EXIT_OK


EXPORT_COLUMNS = [
    "id", "counterparty", "title", "status", "effective_date", "expiration_date",
    "auto_renew", "notice_period_days", "renewal_deadline", "governing_law",
    "value_amount", "value_currency", "next_due", "needs_review", "vaulted",
]


def export_rows(records: List[Tuple[str, Path, JSONObj]], as_of: dt.date) -> List[JSONObj]:
    """One flat reporting row per deal (the basis for csv/md/json export)."""
    rows: List[JSONObj] = []
    for rid, _d, rec in records:
        term = _as_dict(rec.get("term"))
        rw = _as_dict(term.get("renewal_window"))
        val = _as_dict(rec.get("value"))
        upcoming = sorted(
            d for d in (parse_date(o.get("due")) for o in _dicts(rec.get("obligations")))
            if d is not None and d >= as_of
        )
        rows.append(
            {
                "id": rid,
                "counterparty": primary_counterparty(rec),
                "title": rec.get("title") or "",
                "status": rec.get("status") or "",
                "effective_date": rec.get("effective_date") or "",
                "expiration_date": rec.get("expiration_date") or "",
                "auto_renew": term.get("auto_renew"),
                "notice_period_days": term.get("notice_period_days"),
                "renewal_deadline": rw.get("deadline", ""),
                "governing_law": rec.get("governing_law") or "",
                "value_amount": val.get("amount"),
                "value_currency": val.get("currency") or "",
                "next_due": upcoming[0].isoformat() if upcoming else "",
                "needs_review": len(review_flags(rec)),
                "vaulted": bool(_as_dict(rec.get("source")).get("vaulted", False)),
            }
        )
    return rows


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _markdown_report(rows: List[JSONObj], totals: Dict[str, float]) -> str:
    out = io.StringIO()
    out.write(f"# Contract portfolio ({len(rows)} deal(s))\n\n")
    if totals:
        out.write("**Total value:** " + ", ".join(f"{round(v, 2):,} {k}" for k, v in sorted(totals.items())) + "\n\n")
    out.write("| " + " | ".join(EXPORT_COLUMNS) + " |\n")
    out.write("|" + "|".join("---" for _ in EXPORT_COLUMNS) + "|\n")
    for r in rows:
        out.write("| " + " | ".join(_cell(r[c]).replace("|", "\\|") for c in EXPORT_COLUMNS) + " |\n")
    return out.getvalue()


def cmd_export(args: argparse.Namespace) -> int:
    """Export the register as csv | md | json for stakeholders / spreadsheets / reports."""
    vault = resolve_vault(args)
    as_of = dt.date.today()
    exp_before = parse_date(args.expiring_before) if getattr(args, "expiring_before", None) else None
    if getattr(args, "expiring_before", None) and exp_before is None:
        raise UsageError(f"invalid --expiring-before date: {args.expiring_before!r}")
    selected: List[Tuple[str, Path, JSONObj]] = []
    for rid, d, rec in load_all_records(vault):
        if exp_before is not None:
            e = parse_date(rec.get("expiration_date"))
            if e is None or e >= exp_before:
                continue
        if getattr(args, "needs_review", False) and not review_flags(rec):
            continue
        selected.append((rid, d, rec))
    rows = export_rows(selected, as_of)
    fmt = getattr(args, "format", None) or ("json" if getattr(args, "json", False) else "csv")
    if getattr(args, "json", False):
        fmt = "json"
    _why(args, "export", f"deals={len(rows)}", f"format={fmt}")
    if fmt == "json":
        _emit_json({"generated_at": _now_iso(), "count": len(rows), "columns": EXPORT_COLUMNS, "deals": rows})
        return EXIT_OK
    if fmt == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(EXPORT_COLUMNS)
        for r in rows:
            writer.writerow([_cell(r[c]) for c in EXPORT_COLUMNS])
        return EXIT_OK
    # markdown report (with a value summary header)
    totals: Dict[str, float] = {}
    for r in rows:
        amt = r["value_amount"]
        if isinstance(amt, (int, float)) and not isinstance(amt, bool):
            cur = str(r["value_currency"] or "(unknown)")
            totals[cur] = totals.get(cur, 0.0) + float(amt)
    sys.stdout.write(_markdown_report(rows, totals))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    findings: List[str] = []
    checked = 0
    for rid, d, rec in load_all_records(vault):
        src = _as_dict(rec.get("source"))
        if src.get("vaulted") and src.get("path") and src.get("sha256"):
            f = d / str(src["path"])
            if not f.exists():
                findings.append(f"{rid}: vaulted source missing ({src['path']})")
                continue
            actual = sha256_file(f)
            checked += 1
            if actual != src["sha256"]:
                findings.append(f"{rid}: sha256 mismatch (stored {str(src['sha256'])[:12]}, actual {actual[:12]})")
    dirty = ""
    if (vault / ".git").exists():
        status = _git(vault, "status", "--porcelain", check=False)
        dirty = status.stdout.strip()
        if dirty:
            findings.append(f"git working tree not clean ({len(dirty.splitlines())} change(s))")
    ok = not findings
    _why(args, "verify", f"records={len(load_all_records(vault))}", f"sha_checked={checked}", f"git_dirty={bool(dirty)}")
    if getattr(args, "json", False):
        _emit_json({"ok": ok, "checked": checked, "findings": findings})
    elif ok:
        _out(_green(f"OK -- {checked} source file(s) verified, git tree clean"))
    else:
        for msg in findings:
            _eprint(_red(f"FAIL: {msg}"))
    return EXIT_OK if ok else EXIT_FAIL


def cmd_review(args: argparse.Namespace) -> int:
    """Deterministic 'verify, not trust' worklist: deals with unidentified, LLM-derived,
    or low-confidence fields. Never calls an LLM; re-ingest with --llm to improve them."""
    vault = resolve_vault(args)
    threshold = getattr(args, "threshold", None)
    threshold = REVIEW_THRESHOLD if threshold is None else float(threshold)
    records = load_all_records(vault)
    flagged = [(rid, rec, review_flags(rec, threshold)) for rid, _d, rec in records]
    flagged = [t for t in flagged if t[2]]
    # --strict makes review a CI gate (exit 1 when anything needs review), like verify.
    rc = EXIT_FAIL if (flagged and getattr(args, "strict", False)) else EXIT_OK
    _why(args, "review", f"scanned={len(records)}", f"needs_review={len(flagged)}", f"threshold={threshold}", f"strict={getattr(args, 'strict', False)}")
    if getattr(args, "json", False):
        _emit_json(
            {
                "threshold": threshold,
                "count": len(flagged),
                "deals": [
                    {"id": rid, "counterparty": primary_counterparty(rec), "flags": fl}
                    for rid, rec, fl in flagged
                ],
            }
        )
        return rc
    if not flagged:
        _out(_green(f"Nothing needs review (confidence threshold {threshold})."))
        return EXIT_OK
    for rid, _rec, fl in flagged:
        _out(_bold(rid) + _dim(f"  ({len(fl)} field(s))"))
        for f in fl:
            label = f" ({f['label']})" if f.get("label") and f["label"] != f["field"] else ""
            _out(f"   - {f['field']}{label}: {_yellow(', '.join(f['reasons']))}  [{f['source']}, conf {f['confidence']}]")
    _out(_dim(f"{len(flagged)} deal(s) need review. Accept a field with `contract-vault accept <deal> <field> [--value V]`, or improve via `ingest --llm`."))
    return rc


# Scalar fields a human can accept/override; map to where they live + how to type a value.
ACCEPTABLE_FIELDS = {
    "effective_date", "expiration_date", "governing_law", "value",
    "term.length", "term.auto_renew", "term.notice_period_days",
}
SCHEDULE_FIELDS = {"expiration_date", "term.notice_period_days", "term.auto_renew"}


def _apply_value(record: JSONObj, field: str, raw: str) -> None:
    """Set a scalar field from a human-supplied --value, with the right typing/parsing."""
    if field == "value":
        amount, currency = parse_money(raw)
        record["value"] = {"raw": raw, "amount": amount, "currency": currency}
    elif field == "governing_law":
        record["governing_law"] = raw
    elif field in ("effective_date", "expiration_date"):
        d = parse_date(raw)
        if d is None:
            raise UsageError(f"--value {raw!r} is not a recognizable date for {field}")
        record[field] = d.isoformat()
    elif field == "term.length":
        _dict_at(record, "term")["length"] = raw
    elif field == "term.auto_renew":
        b = _parse_bool(raw)
        if b is None:
            raise UsageError(f"--value {raw!r} is not a boolean (use true/false) for {field}")
        _dict_at(record, "term")["auto_renew"] = b
    elif field == "term.notice_period_days":
        m = re.search(r"\d+", raw)
        if not m:
            raise UsageError(f"--value {raw!r} has no day count for {field}")
        _dict_at(record, "term")["notice_period_days"] = int(m.group())
    else:  # pragma: no cover - guarded by ACCEPTABLE_FIELDS
        raise UsageError(f"--value not supported for field {field!r}")


def _accept_field(record: JSONObj, field: str, value: Optional[str]) -> bool:
    """Apply one accept to a record in place; return True if a schedule field changed.

    Marks the target source=manual, confidence=1.0, optionally overriding its value.
    Supports the scalar field_meta keys, parties[i], and obligations[i].
    """
    party_match = re.fullmatch(r"parties\[(\d+)\]", field)
    if party_match:
        idx = int(party_match.group(1))
        parties = _as_list(record.get("parties"))
        if idx >= len(parties) or not isinstance(parties[idx], dict):
            raise UsageError(f"no parties[{idx}] on this record")
        if value is not None:
            parties[idx]["name"] = value
        parties[idx]["source"] = SOURCE_MANUAL
        parties[idx]["confidence"] = 1.0
        return False
    ob_match = re.fullmatch(r"obligations\[(\d+)\]", field)
    if ob_match:
        idx = int(ob_match.group(1))
        obs = _as_list(record.get("obligations"))
        if idx >= len(obs) or not isinstance(obs[idx], dict):
            raise UsageError(f"no obligations[{idx}] on this record")
        if value is not None:
            obs[idx]["description"] = value
        obs[idx]["source"] = SOURCE_MANUAL
        obs[idx]["confidence"] = 1.0
        return False
    if field in ACCEPTABLE_FIELDS:
        if value is not None:
            _apply_value(record, field, value)
        _dict_at(record, "field_meta")[field] = {"source": SOURCE_MANUAL, "confidence": 1.0}
        return field in SCHEDULE_FIELDS
    raise UsageError(
        f"unknown field {field!r}; acceptable: "
        + ", ".join(sorted(ACCEPTABLE_FIELDS))
        + ", parties[i], obligations[i]"
    )


def _load_accept_file(path: Path) -> List[Tuple[str, str, Optional[str]]]:
    """Parse a bulk-accept file: a list of {deal, field, value?} or `review --json` output."""
    data = load_json_file(path)
    out: List[Tuple[str, str, Optional[str]]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or "deal" not in item or "field" not in item:
                raise UsageError("each --from entry needs 'deal' and 'field'")
            val = item.get("value")
            out.append((str(item["deal"]), str(item["field"]), None if val is None else str(val)))
    elif isinstance(data, dict) and isinstance(data.get("deals"), list):
        for deal in data["deals"]:  # review --json output: accept every listed flag as-is
            if not isinstance(deal, dict) or "id" not in deal:
                raise UsageError("each --from deal needs an 'id'")
            for fl in deal.get("flags", []) or []:
                if not isinstance(fl, dict) or "field" not in fl:
                    raise UsageError("each --from flag needs a 'field'")
                out.append((str(deal["id"]), str(fl["field"]), None))
    else:
        raise UsageError("--from file must be a list of {deal,field,value?} or `review --json` output")
    return out


def cmd_accept(args: argparse.Namespace) -> int:
    """Mark reviewed field(s) as human-verified (source=manual, confidence=1.0), optionally
    overriding values. Recomputes the deadline calendar when a date/term field changes.
    Single (`accept <deal> <field>`) or bulk (`accept --from FILE`). Never calls an LLM."""
    vault = resolve_vault(args)
    from_file = getattr(args, "from_file", None)
    if from_file:
        instructions = _load_accept_file(Path(from_file).expanduser())
    else:
        if not getattr(args, "deal", None) or not getattr(args, "field", None):
            raise UsageError("provide <deal> <field>, or --from FILE for bulk accept")
        instructions = [(args.deal, args.field, getattr(args, "value", None))]
    if not instructions:
        _out(_yellow("nothing to accept"))
        return EXIT_OK

    # Resolve each distinct deal once so multiple edits to one record accumulate.
    resolved: Dict[Path, Tuple[str, JSONObj]] = {}
    plan: List[Tuple[Path, str, Optional[str]]] = []
    for deal, field, value in instructions:
        rid, deal_dir, rec = find_deal(vault, deal)
        if deal_dir not in resolved:
            resolved[deal_dir] = (rid, rec)
        plan.append((deal_dir, field, value))

    schedule_dirty: Dict[Path, bool] = {}
    for deal_dir, field, value in plan:
        _rid, rec = resolved[deal_dir]
        changed = _accept_field(rec, field, value)
        schedule_dirty[deal_dir] = schedule_dirty.get(deal_dir, False) or changed

    paths: List[Path] = []
    for deal_dir, (_rid, rec) in resolved.items():
        if schedule_dirty.get(deal_dir):
            recompute_schedule(rec)
        prov = _as_dict(rec.get("provenance"))
        rec["provenance"] = prov
        prov["last_reviewed_at"] = _now_iso()
        _atomic_write_text(deal_dir / RECORD_FILENAME, _dump_json(rec), mode=0o600)
        paths.append(deal_dir)

    if from_file:
        msg = f"review: bulk accept {len(plan)} field(s) across {len(resolved)} deal(s)"
    else:
        d0, f0, v0 = plan[0]
        msg = f"review: accept {resolved[d0][0]} {f0}" + (f" = {v0}" if v0 is not None else "")
    git_commit(vault, msg, paths=paths)
    _why(args, "accept", f"instructions={len(plan)}", f"deals={len(resolved)}", f"from={from_file or '(single)'}")

    remaining = sum(len(review_flags(rec)) for _rid, rec in resolved.values())
    if getattr(args, "json", False):
        _emit_json(
            {
                "accepted": len(plan),
                "deals": [resolved[dd][0] for dd in resolved],
                "marked": "manual",
                "remaining_flags": remaining,
            }
        )
    elif from_file:
        _out(_green(f"Accepted {len(plan)} field(s) across {len(resolved)} deal(s)") + _dim(f"  ({remaining} still need review)"))
    else:
        d0, f0, v0 = plan[0]
        what = f" = {v0}" if v0 is not None else " (accepted as-is)"
        _out(_green(f"Marked {resolved[d0][0]} :: {f0} as manual{what}") + _dim(f"  ({remaining} field(s) still need review)"))
    return EXIT_OK


def risk_items(records: List[Tuple[str, Path, JSONObj]], as_of: dt.date, within_days: int) -> List[JSONObj]:
    """Renewal-exposure analysis: notice deadlines that are MISSED (passed while the contract
    is still active) or imminent, plus expirations within the window. Unlike `due`, this
    surfaces deadlines that are already in the past -- the highest-stakes case."""
    horizon = as_of + dt.timedelta(days=within_days)
    items: List[JSONObj] = []
    for rid, _d, rec in records:
        cp = primary_counterparty(rec)
        term = _as_dict(rec.get("term"))
        exp = parse_date(rec.get("expiration_date"))
        auto = term.get("auto_renew")
        deadline = parse_date(_as_dict(term.get("renewal_window")).get("deadline"))
        active = exp is None or exp >= as_of
        if not active:
            continue
        # One item per deal: the single most pressing concern, highest priority first.
        item: Optional[JSONObj] = None
        if deadline is not None and deadline < as_of:
            sev = "critical" if auto is True else "warning"
            note = "auto-renewal notice window MISSED" if auto is True else "notice deadline passed"
            item = {"severity": sev, "kind": "renewal_notice", "due": deadline.isoformat(),
                    "days_until": (deadline - as_of).days, "note": note}
        elif deadline is not None and deadline <= horizon:
            item = {"severity": "soon", "kind": "renewal_notice", "due": deadline.isoformat(),
                    "days_until": (deadline - as_of).days, "note": "notice deadline approaching"}
        elif exp is not None and as_of <= exp <= horizon:
            item = {"severity": "soon", "kind": "expiration", "due": exp.isoformat(),
                    "days_until": (exp - as_of).days, "note": "contract expiring"}
        if item is not None:
            item.update({"deal": rid, "counterparty": cp,
                         "expiration": exp.isoformat() if exp else None, "auto_renew": auto})
            items.append(item)
    rank = {"critical": 0, "warning": 1, "soon": 2}
    items.sort(key=lambda it: (rank.get(str(it["severity"]), 3), str(it["due"])))
    return items


def cmd_risk(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    within = parse_within(args.within) if getattr(args, "within", None) else 30
    as_of = parse_date(args.as_of) if getattr(args, "as_of", None) else dt.date.today()
    if getattr(args, "as_of", None) and as_of is None:
        raise UsageError(f"invalid --as-of date: {args.as_of!r}")
    assert as_of is not None
    items = risk_items(load_all_records(vault), as_of, within)
    n_critical = sum(1 for it in items if it["severity"] == "critical")
    rc = EXIT_FAIL if (n_critical and getattr(args, "strict", False)) else EXIT_OK
    _why(args, "risk", f"as_of={as_of.isoformat()}", f"within_days={within}", f"items={len(items)}", f"critical={n_critical}")
    if getattr(args, "json", False):
        _emit_json({"as_of": as_of.isoformat(), "within_days": within, "count": len(items), "critical": n_critical, "items": items})
        return rc
    if not items:
        _out(_green(f"No at-risk deadlines within {within} days (as of {as_of.isoformat()})."))
        return EXIT_OK
    colors = {"critical": "31", "warning": "33", "soon": "36"}
    for it in items:
        tag = _c(str(it["severity"]).upper(), colors[str(it["severity"])])
        days = int(it["days_until"])
        when = f"{it['due']} ({'+' if days >= 0 else ''}{days}d)"
        _out(f"  [{tag}] {when}  {it['kind']}  {it['counterparty']}  -- {it['note']}")
    if n_critical:
        _out(_red(f"{n_critical} CRITICAL (auto-renewal notice window missed)."))
    return rc


def cmd_history(args: argparse.Namespace) -> int:
    """Show the git history of a deal (ingest + each accept is a commit)."""
    vault = resolve_vault(args)
    rid, deal_dir, _rec = find_deal(vault, args.deal)
    rel = deal_dir.relative_to(vault).as_posix()
    proc = _git(vault, "log", "--format=%h%x09%aI%x09%an%x09%s", "--", rel, check=False)
    entries: List[JSONObj] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            entries.append({"commit": parts[0], "date": parts[1], "author": parts[2], "message": parts[3]})
    _why(args, "history", f"deal={rid}", f"commits={len(entries)}")
    if getattr(args, "json", False):
        _emit_json({"deal": rid, "count": len(entries), "history": entries})
        return EXIT_OK
    if not entries:
        _out(_dim("(no history)"))
        return EXIT_OK
    _out(_bold(rid))
    for e in entries:
        _out(f"  {_yellow(str(e['commit']))}  {str(e['date'])[:10]}  {e['message']}")
    return EXIT_OK


def find_obligation(record: JSONObj, ref: str) -> JSONObj:
    """Resolve an obligation within a record by id, unique id-prefix, or [i] index."""
    obs = _dicts(record.get("obligations"))
    idx_match = re.fullmatch(r"\[?(\d+)\]?", ref)
    if idx_match:
        i = int(idx_match.group(1))
        if i < len(obs):
            return obs[i]
    exact = [o for o in obs if o.get("id") == ref]
    if len(exact) == 1:
        return exact[0]
    prefix = [o for o in obs if str(o.get("id", "")).startswith(ref)]
    if len(prefix) == 1:
        return prefix[0]
    if not obs:
        raise NotFoundError("this deal has no obligations")
    raise UsageError(f"no unique obligation matching {ref!r}; run `contract-vault get <deal>` to list ids")


def cmd_obligation(args: argparse.Namespace) -> int:
    """Track an obligation: status (open/done/waived), owner, recurrence, and reminders.
    Deterministic and local -- commits to the vault. Never calls an LLM."""
    vault = resolve_vault(args)
    rid, deal_dir, rec = find_deal(vault, args.deal)
    ob = find_obligation(rec, args.id)
    # An index-resolved obligation may lack an "id"; stamp the stable one so the
    # commit message / output / JSON below never raise KeyError on a hand-edited record.
    if not ob.get("id"):
        ob["id"] = obligation_id(ob)
    status = getattr(args, "status", None)
    owner = getattr(args, "owner", None)
    recurrence = getattr(args, "recurrence", None)
    reminders_arg = getattr(args, "reminders", None)
    if status is None and owner is None and recurrence is None and reminders_arg is None:
        raise UsageError("provide --status, --owner, --recurrence, and/or --reminders")
    if status is not None:
        if status not in OBLIGATION_STATUSES:
            raise UsageError(f"invalid --status {status!r}; choose from {', '.join(OBLIGATION_STATUSES)}")
        ob["status"] = status
    if owner is not None:
        ob["owner"] = owner or None  # empty string clears the owner
    if recurrence is not None:
        if recurrence == "none":
            ob.pop("recurrence", None)
        else:
            ob["recurrence"] = recurrence  # argparse choices guarantee validity
    if reminders_arg is not None:
        if reminders_arg.strip() == "":
            ob.pop("reminders", None)  # fall back to the type default
        else:
            try:
                vals = [int(x) for x in reminders_arg.replace(" ", "").split(",") if x]
            except ValueError:
                raise UsageError("--reminders must be comma-separated day counts, e.g. 30,7")
            if any(v < 0 for v in vals):
                raise UsageError("--reminders must be non-negative day counts")
            ob["reminders"] = sorted(set(vals), reverse=True)
    prov = _as_dict(rec.get("provenance"))
    rec["provenance"] = prov
    prov["last_reviewed_at"] = _now_iso()
    _atomic_write_text(deal_dir / RECORD_FILENAME, _dump_json(rec), mode=0o600)
    git_commit(vault, f"obligation: {rid} {ob['id']} status={ob.get('status')}", paths=[deal_dir])
    _why(args, "obligation", f"deal={rid}", f"id={ob['id']}", f"status={ob.get('status')}",
         f"owner={ob.get('owner')}", f"recurrence={ob.get('recurrence')}", f"reminders={ob.get('reminders')}")
    if getattr(args, "json", False):
        _emit_json({"deal": rid, "id": ob["id"], "type": ob.get("type"), "status": ob.get("status"),
                    "owner": ob.get("owner"), "recurrence": ob.get("recurrence"), "reminders": ob.get("reminders"),
                    "due": ob.get("due")})
    else:
        bits = [f"status={ob.get('status')}"]
        if ob.get("owner"):
            bits.append(f"owner={ob.get('owner')}")
        if ob.get("recurrence"):
            bits.append(f"recurrence={ob.get('recurrence')}")
        if ob.get("reminders"):
            bits.append(f"reminders={ob.get('reminders')}")
        _out(_green(f"{rid} :: {ob['id']} [{ob.get('type')}] -> " + " ".join(bits)))
    return EXIT_OK


_BUILTIN_REMINDER_DEFAULTS = {t: [_suggest_lead_days(t)] for t in ("expiration", "renewal_notice", "obligation")}


def cmd_config(args: argparse.Namespace) -> int:
    """Get/set vault-wide settings. Currently: `config reminders` -- default reminder
    lead-times applied to every obligation in the corpus (per-type, overridable per
    obligation). Stored in .contract-vault.json; commits to the vault."""
    vault = resolve_vault(args)
    if args.topic != "reminders":
        raise UsageError(f"unknown config topic {args.topic!r} (only 'reminders')")
    cfg = load_vault_config(vault)
    rd = cfg.get("reminder_defaults")
    rd = dict(rd) if isinstance(rd, dict) else {}

    set_arg = getattr(args, "set", None)
    clearing = getattr(args, "clear", False)
    if set_arg is not None or clearing:
        if not getattr(args, "type", None):
            raise UsageError("--set/--clear require --type {expiration,renewal_notice,obligation,default}")
        if clearing:
            rd.pop(args.type, None)
        else:
            assert set_arg is not None
            try:
                vals = [int(x) for x in set_arg.replace(" ", "").split(",") if x]
            except ValueError:
                raise UsageError("--set must be comma-separated day counts, e.g. 60,30,7")
            if not vals or any(v < 0 for v in vals):
                raise UsageError("--set must be one or more non-negative day counts")
            rd[args.type] = sorted(set(vals), reverse=True)
        cfg["reminder_defaults"] = rd
        save_vault_config(vault, cfg)
        git_commit(vault, f"config: reminder default {args.type}=" + ("cleared" if clearing else str(rd.get(args.type))),
                   paths=[vault / VAULT_CONFIG_NAME])

    _why(args, "config", f"topic=reminders", f"configured={list(rd)}")
    if getattr(args, "json", False):
        _emit_json({"reminder_defaults": rd, "builtin_fallback": _BUILTIN_REMINDER_DEFAULTS})
        return EXIT_OK
    _out(_bold("Reminder defaults (corpus-wide; overridable per obligation):"))
    if not rd:
        _out(_dim("  (none set -- using built-in defaults below)"))
    for typ, leads in rd.items():
        _out(f"  {typ}: {leads}")
    _out(_dim("  built-in fallback: " + ", ".join(f"{t}={l}" for t, l in _BUILTIN_REMINDER_DEFAULTS.items())))
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    tmp = tempfile.mkdtemp(prefix="contract-vault-demo-")
    try:
        vault = Path(tmp) / "vault"
        ns = argparse.Namespace(path=str(vault), json=False, why=getattr(args, "why", False))
        cmd_init(ns)
        _out("")
        _out(_bold("1) Ingest bundled extract-cli sample payloads (no extract-cli needed):"))
        for name, payload in DEMO_EXTRACTS:
            deal, created = store_record(
                vault, json.loads(json.dumps(payload)),
                counterparty_override=None, name_override=None, local_source=None,
            )
            _out(f"   - {('ingested' if created else 'skipped')} {deal}")
        _out("")
        _out(_bold("2) find --auto-renew --value-gt 50000:"))
        find_ns = argparse.Namespace(
            vault=str(vault), json=False, why=False, counterparty=None, governing_law=None,
            expiring_before=None, value_gt=50000.0, auto_renew=True, text=None,
        )
        cmd_find(find_ns)
        _out("")
        _out(_bold("3) due --within 365d (table):"))
        due_ns = argparse.Namespace(vault=str(vault), json=False, why=False, within="365d", as_of="2026-01-01", format="table")
        cmd_due(due_ns)
        _out("")
        _out(_bold("4) due --within 365d --format ics  (first lines):"))
        ics = build_ics(upcoming_obligations(vault, within_days=365, as_of=dt.date(2026, 1, 1)))
        for line in ics.split("\r\n")[:8]:
            _out(f"   {line}")
        _out("")
        _out(_green("Demo complete. This entire flow is deterministic and ran without extract-cli or any LLM."))
    finally:
        _rmtree_force(tmp)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Hidden shell completion: `contract-vault __complete <words...>`
# ---------------------------------------------------------------------------

SUBCOMMANDS = [
    "init", "ingest", "list", "get", "show", "find", "search",
    "due", "obligations", "stats", "verify", "review", "accept", "export",
    "risk", "history", "obligation", "remind", "config", "demo",
]
FIND_FLAGS = [
    "--counterparty", "--governing-law", "--currency", "--expiring-before",
    "--value-gt", "--auto-renew", "--needs-review", "--text", "--json",
]


def cmd_complete(words: Sequence[str]) -> int:
    """Emit completion candidates (one per line). Never raises; silent on any error."""
    try:
        words = list(words)
        current = words[-1] if words else ""
        sub = words[0] if words else ""
        candidates: List[str] = []
        if len(words) <= 1:
            candidates = SUBCOMMANDS
        elif sub in ("get", "show", "verify", "accept", "history", "obligation"):
            try:
                vault = resolve_vault(argparse.Namespace())
                candidates = [rid for rid, _d, _r in load_all_records(vault)]
            except VaultError:
                candidates = []
        elif sub in ("find", "search"):
            candidates = list(FIND_FLAGS)
            try:
                vault = resolve_vault(argparse.Namespace())
                candidates += sorted({primary_counterparty(r) for _i, _d, r in load_all_records(vault)})
                candidates += sorted({str(r.get("governing_law")) for _i, _d, r in load_all_records(vault) if r.get("governing_law")})
            except VaultError:
                pass
        elif sub in ("due", "obligations"):
            candidates = ["--within", "--format", "--as-of", "--json", "ics", "json", "table", "30d", "60d", "90d"]
        for c in candidates:
            if c.startswith(current):
                print(c)
        return EXIT_OK
    except Exception:  # completion must never break a shell session
        return EXIT_OK


# ---------------------------------------------------------------------------
# Bundled demo payloads (valid extract-cli output; let demo/tests run offline)
# ---------------------------------------------------------------------------


def _demo_field(value: Any, conf: float = 0.95, source: str = SOURCE_DETERMINISTIC) -> JSONObj:
    return {"value": value, "confidence": conf, "source": source}


DEMO_EXTRACTS: List[Tuple[str, JSONObj]] = [
    (
        "acme-msa",
        {
            "document": {
                "title": "Master Services Agreement",
                "format": "pdf",
                "sha256": "a" * 64,
                "source_path": "/contracts/acme-msa.pdf",
            },
            "parties": [
                {"name": "Acme Corporation", "role": "Customer", "confidence": 0.98, "source": SOURCE_DETERMINISTIC},
                {"name": "Globex LLC", "role": "Provider", "confidence": 0.97, "source": SOURCE_DETERMINISTIC},
            ],
            "dates": {
                "effective": _demo_field("2025-03-01"),
                "expiration": _demo_field("2026-02-28"),
            },
            "term": {
                "length": _demo_field("1 year"),
                "auto_renew": _demo_field(True),
                "notice_period_days": _demo_field(60),
            },
            "governing_law": _demo_field("Delaware"),
            "clauses": [],
            "defined_terms": [],
            "value": _demo_field("$120,000"),
            "obligations": [
                {"text": "Customer shall pay the annual fee by 2025-04-15.", "confidence": 0.8, "source": SOURCE_DETERMINISTIC},
            ],
            "_meta": {"extractor_version": "extract-cli 1.4.0", "tiers_used": ["deterministic"], "llm_used": False},
        },
    ),
    (
        "initech-nda",
        {
            "document": {
                "title": "Mutual Non-Disclosure Agreement",
                "format": "docx",
                "sha256": "b" * 64,
                "source_path": "/contracts/initech-nda.docx",
            },
            "parties": [
                {"name": "Initech Inc", "role": "Disclosing Party", "confidence": 0.95, "source": SOURCE_DETERMINISTIC},
            ],
            "dates": {
                "effective": _demo_field("2025-06-15"),
                "expiration": _demo_field("2027-06-14"),
            },
            "term": {
                "length": _demo_field("2 years"),
                "auto_renew": _demo_field(False),
                "notice_period_days": _demo_field(None, 0.0, SOURCE_NONE),
            },
            "governing_law": _demo_field("California"),
            "clauses": [],
            "defined_terms": [],
            "value": _demo_field(None, 0.0, SOURCE_NONE),
            "_meta": {"extractor_version": "extract-cli 1.4.0", "tiers_used": ["deterministic"], "llm_used": False},
        },
    ),
]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--vault", metavar="DIR", help="vault directory (default: discover upward / $CONTRACT_VAULT_DIR)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    p.add_argument("--why", action="store_true", help="structured explanation on stderr")
    p.add_argument("--no-color", action="store_true", help="disable ANSI color (also honors NO_COLOR/FORCE_COLOR)")
    p.add_argument("-q", "--quiet", "--silent", dest="quiet", action="store_true", help="suppress non-error output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contract-vault",
        description="Register signed contracts and surface their renewals, notice deadlines, and obligations. Git-backed, local-first.",
    )
    parser.add_argument("-V", "--version", action="version", version=f"contract-vault {__version__}")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_init = sub.add_parser("init", help="create / initialize an executed-contract vault")
    _add_common(p_init)
    p_init.add_argument("path", nargs="?", help="vault directory (default: current directory)")
    p_init.add_argument("--encrypt", action="store_true", help="encrypt records + sources at rest via git-crypt (requires git-crypt)")
    p_init.set_defaults(func=cmd_init)

    p_ing = sub.add_parser("ingest", help="ingest extract-cli JSON (shell out to `extract`, or read piped JSON via '-')")
    _add_common(p_ing)
    p_ing.add_argument("file", help="document to extract+store, a '.json' extract payload, or '-' for piped JSON")
    p_ing.add_argument("--counterparty", help="override counterparty (folder); default: first party")
    p_ing.add_argument("--name", help="override deal name (folder); default: document title")
    p_ing.add_argument("--llm", action="store_true", help="forward --llm to the extract step (opt-in; delegated)")
    p_ing.set_defaults(func=cmd_ingest)

    p_list = sub.add_parser("list", help="list stored deals")
    _add_common(p_list)
    p_list.set_defaults(func=cmd_list)

    for alias in ("get", "show"):
        p_get = sub.add_parser(alias, help="print one stored record")
        _add_common(p_get)
        p_get.add_argument("id", help="deal id (path, leaf name, or unique prefix)")
        p_get.set_defaults(func=cmd_get)

    for alias in ("find", "search"):
        p_find = sub.add_parser(alias, help="query deals by any field")
        _add_common(p_find)
        p_find.add_argument("text", nargs="?", help="full-text query")
        p_find.add_argument("--counterparty", help="match counterparty / party name")
        p_find.add_argument("--governing-law", dest="governing_law", help="match governing law")
        p_find.add_argument("--currency", help="match value currency, e.g. USD/EUR/GBP (or 'none' for unpriced); pair with --value-gt for currency-aware thresholds")
        p_find.add_argument("--expiring-before", dest="expiring_before", metavar="DATE", help="expiration date before DATE")
        p_find.add_argument("--value-gt", dest="value_gt", type=float, metavar="N", help="value amount greater than N (compare within a currency by adding --currency)")
        p_find.add_argument("--auto-renew", dest="auto_renew", action="store_true", help="only auto-renewing deals")
        p_find.add_argument("--needs-review", dest="needs_review", action="store_true", help="only deals with unidentified/llm-derived/low-confidence fields")
        p_find.set_defaults(func=cmd_find)

    for alias in ("due", "obligations"):
        p_due = sub.add_parser(alias, help="project upcoming date/obligation actions (ics|json|table)")
        _add_common(p_due)
        p_due.add_argument("--within", default="90d", help="window, e.g. 30d/60d/90d (default 90d)")
        p_due.add_argument("--format", choices=["ics", "json", "table"], help="output format (default table; --json forces json)")
        p_due.add_argument("--as-of", dest="as_of", metavar="DATE", help="reference 'today' (default: actual today)")
        p_due.add_argument("--status", choices=["open", "done", "waived", "all"], help="filter by obligation status (default: open)")
        p_due.add_argument("--type", help="filter by obligation type (expiration|renewal_notice|obligation)")
        p_due.add_argument("--owner", help="filter by assigned owner")
        p_due.set_defaults(func=cmd_due)

    p_config = sub.add_parser("config", help="get/set vault-wide settings (currently: corpus-wide reminder defaults)")
    _add_common(p_config)
    p_config.add_argument("topic", choices=["reminders"], help="config topic")
    p_config.add_argument("--type", dest="type", choices=["expiration", "renewal_notice", "obligation", "default"], help="obligation type (or 'default' catch-all)")
    p_config.add_argument("--set", dest="set", metavar="DAYS", help="set reminder lead-times for --type, comma-separated (e.g. 60,30,7)")
    p_config.add_argument("--clear", action="store_true", help="clear the default for --type")
    p_config.add_argument("--show", action="store_true", help="show current reminder defaults (default action)")
    p_config.set_defaults(func=cmd_config)

    p_remind = sub.add_parser("remind", help="obligations whose reminder window is open right now (digest for cron/agents)")
    _add_common(p_remind)
    p_remind.add_argument("--as-of", dest="as_of", metavar="DATE", help="reference 'today' (default: actual today)")
    p_remind.add_argument("--format", choices=["ics", "json", "table"], help="output format (default table; --json forces json)")
    p_remind.add_argument("--status", choices=["open", "done", "waived", "all"], help="filter by status (default: open)")
    p_remind.add_argument("--type", help="filter by obligation type")
    p_remind.add_argument("--owner", help="filter by assigned owner")
    p_remind.add_argument("--strict", action="store_true", help="exit 1 if any reminder is due (for cron/agent gating)")
    p_remind.set_defaults(func=cmd_remind)

    p_stats = sub.add_parser("stats", help="portfolio statistics")
    _add_common(p_stats)
    p_stats.add_argument("--as-of", dest="as_of", metavar="DATE", help="reference 'today' for 'expiring soon' (default: actual today)")
    p_stats.set_defaults(func=cmd_stats)

    p_export = sub.add_parser("export", help="export the register as csv | md | json (for spreadsheets / reports)")
    _add_common(p_export)
    p_export.add_argument("--format", choices=["csv", "md", "json"], help="output format (default csv; --json forces json)")
    p_export.add_argument("--expiring-before", dest="expiring_before", metavar="DATE", help="only deals expiring before DATE")
    p_export.add_argument("--needs-review", dest="needs_review", action="store_true", help="only deals with review flags")
    p_export.set_defaults(func=cmd_export)

    p_verify = sub.add_parser("verify", help="integrity check (source sha256 + git state)")
    _add_common(p_verify)
    p_verify.set_defaults(func=cmd_verify)

    p_review = sub.add_parser("review", help="list fields needing review (unidentified / llm-derived / low-confidence)")
    _add_common(p_review)
    p_review.add_argument("--threshold", type=float, metavar="C", help=f"confidence threshold (default {REVIEW_THRESHOLD})")
    p_review.add_argument("--strict", action="store_true", help="exit 1 if any field needs review (CI gate)")
    p_review.set_defaults(func=cmd_review)

    p_accept = sub.add_parser("accept", help="mark reviewed field(s) as manual (verified); single or bulk via --from")
    _add_common(p_accept)
    p_accept.add_argument("deal", nargs="?", help="deal id (path, leaf name, or unique prefix)")
    p_accept.add_argument("field", nargs="?", help="field to accept, e.g. value, expiration_date, term.auto_renew, parties[1]")
    p_accept.add_argument("--value", help="override value (otherwise accept the current value as reviewed)")
    p_accept.add_argument("--from", dest="from_file", metavar="FILE", help="bulk: a list of {deal,field,value?} or `review --json` output")
    p_accept.set_defaults(func=cmd_accept)

    for alias in ("risk", "at-risk"):
        p_risk = sub.add_parser(alias, help="renewal-exposure: missed / imminent notice deadlines + expirations")
        _add_common(p_risk)
        p_risk.add_argument("--within", default="30d", help="lookahead window for imminent items (default 30d)")
        p_risk.add_argument("--as-of", dest="as_of", metavar="DATE", help="reference 'today' (default: actual today)")
        p_risk.add_argument("--strict", action="store_true", help="exit 1 if any CRITICAL (missed auto-renewal notice)")
        p_risk.set_defaults(func=cmd_risk)

    p_history = sub.add_parser("history", help="show a deal's git history (ingest + each accept)")
    _add_common(p_history)
    p_history.add_argument("deal", help="deal id (path, leaf name, or unique prefix)")
    p_history.set_defaults(func=cmd_history)

    p_ob = sub.add_parser("obligation", help="track an obligation's lifecycle: --status / --owner (see `obligations` for the list)")
    _add_common(p_ob)
    p_ob.add_argument("deal", help="deal id (path, leaf name, or unique prefix)")
    p_ob.add_argument("id", help="obligation id, id-prefix, or [index] (from `get`/`obligations`)")
    p_ob.add_argument("--status", choices=list(OBLIGATION_STATUSES), help="set status (open/done/waived)")
    p_ob.add_argument("--owner", help="set responsible owner (empty string clears it)")
    p_ob.add_argument("--recurrence", choices=["none", *RECURRENCE_FREQS], help="set recurrence (none clears it)")
    p_ob.add_argument("--reminders", metavar="DAYS", help="reminder lead-times, comma-separated, e.g. 30,7 (empty resets to default)")
    p_ob.set_defaults(func=cmd_obligation)

    p_demo = sub.add_parser("demo", help="run the full ingest->find->due flow on bundled fixtures")
    _add_common(p_demo)
    p_demo.set_defaults(func=cmd_demo)

    return parser


# ---------------------------------------------------------------------------
# Machine-readable catalog (`contract-vault --catalog json`)
# ---------------------------------------------------------------------------
# The suite discovery contract: agents call `contract-vault --catalog json` at
# startup to learn every command and flag instead of hardcoding them (parallel
# to `extract --catalog json`, `nda-review-cli --catalog json`). Generated by
# walking the live parser, so it can never drift from what the binary accepts.


def _catalog_flags(p: argparse.ArgumentParser) -> List[JSONObj]:
    flags: List[JSONObj] = []
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if not action.option_strings:  # positional argument, not a flag
            continue
        if action.help == argparse.SUPPRESS:
            continue
        default = None if action.default is argparse.SUPPRESS else action.default
        flags.append({
            "name": action.option_strings[-1],
            "aliases": list(action.option_strings[:-1]),
            "help": action.help or "",
            "required": bool(action.required),
            "default": default,
            "choices": list(action.choices) if action.choices else None,
        })
    return flags


def _subcommand_help(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> Dict[str, str]:
    """Map each registered command name to its one-line help. argparse keeps the
    per-choice help only on the subparsers action's pseudo-actions."""
    out: Dict[str, str] = {}
    getter = getattr(sub, "_get_subactions", None)
    if callable(getter):
        for pseudo in getter():
            out[str(getattr(pseudo, "dest", ""))] = str(getattr(pseudo, "help", "") or "")
    return out


def build_catalog() -> JSONObj:
    """The payload emitted by `contract-vault --catalog json`."""
    parser = build_parser()
    sub: Optional["argparse._SubParsersAction[argparse.ArgumentParser]"] = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    commands: List[JSONObj] = []
    if sub is not None:
        help_by_name = _subcommand_help(sub)
        for name, subparser in sub.choices.items():
            commands.append({
                "name": name,
                "help": help_by_name.get(name, ""),
                "flags": _catalog_flags(subparser),
            })
    return {
        "name": "contract-vault",
        "bin": "contract-vault",
        "version": __version__,
        "description": (
            "Git-backed register for signed contracts: register executed deals, then "
            "surface renewals, notice deadlines, and obligations as a portfolio and an "
            ".ics calendar."
        ),
        "commands": commands,
        "exitCodes": {
            "0": "success",
            "1": "failure or findings (integrity check failed; or a --strict review/risk gate tripped)",
            "2": "bad usage (unknown command/flag, missing or invalid argument)",
        },
    }


def _set_output_flags(args: argparse.Namespace) -> None:
    global _NO_COLOR, _QUIET
    if getattr(args, "no_color", False):
        _NO_COLOR = True
    if getattr(args, "quiet", False):
        _QUIET = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _configure_streams()

    # Hidden completion handled before argparse so partial/odd args never error.
    if argv and argv[0] == "__complete":
        return cmd_complete(argv[1:])

    # `contract-vault --catalog json`: the suite discovery contract. Intercepted
    # before argparse so it works as a bare global flag, not a subcommand.
    if "--catalog" in argv:
        idx = argv.index("--catalog")
        fmt = argv[idx + 1] if idx + 1 < len(argv) else "json"
        if fmt != "json":
            _eprint(_red(f"error: unknown --catalog format {fmt!r}; supported: json"))
            return EXIT_USAGE
        print(json.dumps(build_catalog(), indent=2, ensure_ascii=True))
        return EXIT_OK

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else (EXIT_OK if code is None else EXIT_USAGE)

    _set_output_flags(args)

    if not getattr(args, "command", None) or not hasattr(args, "func"):
        parser.print_help()
        return EXIT_USAGE

    try:
        return int(args.func(args))
    except UsageError as exc:
        _eprint(_red(f"error: {exc}"))
        return EXIT_USAGE
    except NotFoundError as exc:
        _eprint(_red(f"error: {exc}"))
        return EXIT_FAIL
    except VaultError as exc:
        _eprint(_red(f"error: {exc}"))
        return EXIT_FAIL
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK
    except OSError as exc:
        # Filesystem/git I/O failures (e.g. ENAMETOOLONG, permissions) -> clean error, no traceback.
        _eprint(_red(f"error: {exc}"))
        return EXIT_FAIL
    except KeyboardInterrupt:
        _eprint("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort backstop: no traceback for malformed state
        # A malformed/hand-edited record.json or vault state can surface unexpected
        # errors deep in a command; surface a clean message instead of a traceback.
        # This is a runtime failure (exit 1), not a bad invocation (exit 2): exit 2
        # is reserved for argparse/UsageError, matching the sibling handlers above.
        _eprint(_red(f"error: {exc}"))
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
