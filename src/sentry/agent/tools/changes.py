"""SEN-18 — change history tool.

Two sources behind one tool, so the model asks "what changed?" once rather than
reasoning about CloudTrail and GitHub as separate concerns.

The output deliberately includes `changed_files` for commits and the affected
resource for deployments. Those are what let the model connect a change to a
failure — "the commit touched api/handler.py and the stack trace is in
api/handler.py" is evidence; "a commit happened 8 minutes earlier" is not.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

from ..config import Config
from ...common.logging import get_logger, log_event
from .base import error_result

logger = get_logger("tool.changes")

_ct = boto3.client("cloudtrail", region_name=Config.REGION)
_secrets = boto3.client("secretsmanager", region_name=Config.REGION)

MAX_LOOKBACK_HOURS = 24
DEFAULT_LOOKBACK_HOURS = 24
MAX_COMMITS = 10
GITHUB_TIMEOUT_S = 8

# CloudTrail event names that represent a deploy or config change to this app.
# lookup_events allows ONE attribute filter per call, so we loop.
DEPLOY_EVENTS = [
    "UpdateFunctionCode20150331v2",
    "UpdateFunctionConfiguration20150331v2",
    "UpdateAlias20150331",
    "PublishVersion20150331",
    "PutRolePolicy",
    "AttachRolePolicy",
    "UpdateEventSourceMapping",
    "PutMetricAlarm",
]

_token_cache: str | None = None

TOOL_SPEC = {
    "toolSpec": {
        "name": "get_recent_changes",
        "description": (
            "List changes to the application and its infrastructure shortly before "
            "the incident: code deployments, configuration updates, permission "
            "changes and recent commits with the files they touched. IMPORTANT: a "
            "change appearing here does not mean it caused the incident. Use it to "
            "check whether a specific change could explain the failure you already "
            "observed — whether it touched the code that threw, or altered the "
            "permission that was denied. If nothing connects a change to the "
            "observed failure, report that no change is implicated."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["deployments", "commits", "both"],
                        "description": (
                            "'deployments' covers AWS deploy and config events, "
                            "'commits' covers source changes with file paths."
                        ),
                    },
                    "lookback_hours": {
                        "type": "integer",
                        "description": "How far back to look. Default 24, capped at 72.",
                    },
                },
                "required": ["source"],
            }
        },
    }
}




# --------------------------------------------------------------------------- #
# CloudTrail
# --------------------------------------------------------------------------- #

def _is_own_infrastructure(name: str) -> bool:
    """Sentry's own components, which are never evidence about the target app."""
    return any(own in name for own in Config.OWN_RESOURCES)


def _relevant(event: dict) -> bool:
    """Keep only events touching the target app's resources.

    CloudTrail is account-wide with no resource-level IAM, so filtering happens
    here. Without it the model would see other teams' deploys as evidence.

    Sentry's own deploys are excluded as well: they carry the same name prefix,
    they cannot cause a target-app failure, and during an evaluation sweep they
    are the single largest source of irrelevant changes — the agent would spend
    context reading about itself being redeployed.
    """
    resources = event.get("Resources") or []
    for r in resources:
        name = r.get("ResourceName") or ""
        if "sentry-capstone" in name and not _is_own_infrastructure(name):
            return True

    if resources:
        # Named resources are authoritative; if every one of them was ours,
        # do not fall through to the raw payload and re-admit the event.
        if any("sentry-capstone" in (r.get("ResourceName") or "") for r in resources):
            return False

    # Some events carry the target only inside the raw payload.
    raw = event.get("CloudTrailEvent") or ""
    if "sentry-capstone" not in raw:
        return False
    return not _is_own_infrastructure(raw)


def _extract_target(event: dict) -> str | None:
    for r in (event.get("Resources") or []):
        name = r.get("ResourceName") or ""
        if "sentry-capstone" in name:
            return name
    try:
        detail = json.loads(event.get("CloudTrailEvent") or "{}")
        params = detail.get("requestParameters") or {}
        return params.get("functionName") or params.get("roleName") or params.get("name")
    except (json.JSONDecodeError, AttributeError):
        return None


def _fetch_deployments(start: datetime, end: datetime) -> list[dict]:
    changes: list[dict] = []
    seen: set[str] = set()

    for event_name in DEPLOY_EVENTS:
        try:
            resp = _ct.lookup_events(
                StartTime=start,
                EndTime=end,
                LookupAttributes=[{
                    "AttributeKey": "EventName",
                    "AttributeValue": event_name,
                }],
                MaxResults=20,
            )
        except Exception as exc:
            log_event(logger, "warning", f"cloudtrail lookup failed for {event_name}: {exc}")
            continue

        for event in resp.get("Events", []):
            event_id = event.get("EventId")
            if event_id in seen or not _relevant(event):
                continue
            seen.add(event_id)

            occurred = event.get("EventTime")
            changes.append({
                "kind": "deployment",
                "event": event.get("EventName"),
                # ISO only. The epoch duplicate said the same thing again on
                # every turn, and ISO is what lines up with log timestamps.
                "occurred_at_iso": occurred.isoformat() if occurred else None,
                "actor": event.get("Username"),
                "target": _extract_target(event),
            })

    return changes


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #

def _github_token() -> str | None:
    """Fine-grained PAT scoped to the one repo, from Secrets Manager."""
    global _token_cache
    if _token_cache is not None:
        return _token_cache

    secret_id = os.environ.get("GITHUB_TOKEN_SECRET")
    if not secret_id:
        return None
    try:
        resp = _secrets.get_secret_value(SecretId=secret_id)
        raw = resp.get("SecretString") or ""
        try:
            _token_cache = json.loads(raw).get("token", raw)
        except json.JSONDecodeError:
            _token_cache = raw
        return _token_cache
    except Exception as exc:
        log_event(logger, "warning", f"could not read github token: {exc}")
        return None


def _github_get(url: str, token: str) -> list | dict | None:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "sentry-capstone",
    })
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        log_event(logger, "warning", f"github {exc.code} for {url}")
        return None
    except Exception as exc:
        log_event(logger, "warning", f"github request failed: {exc}")
        return None


def _fetch_commits(start: datetime, end: datetime) -> list[dict]:
    repo = os.environ.get("GITHUB_REPO")          # e.g. "gulsher/sentry-capstone"
    token = _github_token()
    if not repo or not token:
        log_event(logger, "info", "github not configured, skipping commits")
        return []

    # datetime.isoformat() renders UTC as "+00:00", and a bare '+' in a query
    # string decodes to a space — GitHub then sees a malformed `since` and the
    # window filter silently matches nothing. Encode the params, and use the
    # Z-suffixed form so there is no '+' to encode in the first place.
    params = urllib.parse.urlencode({
        "since": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "per_page": MAX_COMMITS,
    })
    listing = _github_get(
        f"https://api.github.com/repos/{repo}/commits?{params}", token
    )
    if not isinstance(listing, list):
        return []

    commits: list[dict] = []
    for item in listing[:MAX_COMMITS]:
        sha = item.get("sha", "")
        commit = item.get("commit") or {}
        author = commit.get("author") or {}

        entry = {
            "kind": "commit",
            "sha": sha[:8],
            "message": (commit.get("message") or "").split("\n")[0][:200],
            "author": author.get("name"),
            "occurred_at_iso": author.get("date"),
            "changed_files": [],
        }

        # File PATHS only — full patches are expensive and paths are what let
        # the model connect a change to a stack trace.
        detail = _github_get(f"https://api.github.com/repos/{repo}/commits/{sha}", token)
        if isinstance(detail, dict):
            entry["changed_files"] = [
                f.get("filename") for f in (detail.get("files") or [])[:20]
            ]

        commits.append(entry)

    return commits


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def run_changes(incident: dict, source: str = "both",
                lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> dict:
    triggered = int(incident.get("triggered_at") or time.time())
    hours = max(1, min(int(lookback_hours or DEFAULT_LOOKBACK_HOURS), MAX_LOOKBACK_HOURS))

    end = datetime.fromtimestamp(triggered, tz=timezone.utc)
    start = datetime.fromtimestamp(triggered - hours * 3600, tz=timezone.utc)

    changes: list[dict] = []
    sources_used: list[str] = []

    try:
        if source in ("deployments", "both"):
            changes.extend(_fetch_deployments(start, end))
            sources_used.append("cloudtrail")

        if source in ("commits", "both"):
            commits = _fetch_commits(start, end)
            changes.extend(commits)
            if commits:
                sources_used.append("github")
    except Exception as exc:
        log_event(logger, "warning", f"change lookup failed: {exc}",
                  error_type=type(exc).__name__)
        return error_result(f"change lookup failed: {exc}")

    changes.sort(key=lambda c: c.get("occurred_at_iso") or "", reverse=True)

    log_event(logger, "info", "change lookup complete",
              count=len(changes), hours=hours, sources=sources_used)

    return {
        "window": {
            "from_iso": start.isoformat(),
            "to_iso": end.isoformat(),
            "lookback_hours": hours,
        },
        "sources_searched": sources_used,
        "result_count": len(changes),
        "changes": changes,
        # Repeated here because tool results carry weight with the model.
        "note": (
            "These changes occurred before the incident. Proximity in time is not "
            "evidence of causation — check whether a change actually touches the "
            "code, permission or configuration involved in the observed failure."
        ),
    }