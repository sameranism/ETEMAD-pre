"""Trusted PR review policy; run only from the protected base revision."""
from __future__ import annotations

import json
import os
import re
import subprocess

CONTEXT = "etemad/trusted-guardian"
ROUTINE_FILES = frozenset({
    "backend/modules/processing/classifier.py",
    "backend/modules/processing/deduplicator.py",
    "backend/modules/processing/extractor.py",
    "backend/modules/processing/normalizer.py",
    "frontend/src/styles/globals.css",
})
ELIGIBLE = {"OWNER", "MEMBER", "COLLABORATOR"}


def sensitive(files, count):
    return (not files or len(files) != count or any(
        item.get("filename") not in ROUTINE_FILES
        or item.get("status") not in {"modified"}
        or item.get("previous_filename") is not None
        for item in files
    ))


def reviewer_approved(pr, reviews):
    latest = {}
    for review in sorted(reviews, key=lambda item: item["id"]):
        user = review["user"]
        login = user["login"].casefold()
        if user.get("type") != "User" or login == pr["user"]["login"].casefold():
            continue
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[login] = review
    return any(
        review["state"] == "APPROVED"
        and review["commit_id"] == pr["head"]["sha"]
        and review["author_association"] in ELIGIBLE
        for review in latest.values()
    ) and not any(
        review["state"] == "CHANGES_REQUESTED"
        and review["author_association"] in ELIGIBLE
        for review in latest.values()
    )


def snapshot(pr, expected, repo, *, open_only=True):
    if (pr["head"]["sha"] != expected or pr["base"]["ref"] != "main"
            or pr["base"]["repo"]["full_name"] != repo
            or (open_only and (pr["state"] != "open" or pr["draft"]))):
        raise ValueError("PR revision, base or state changed")
    return (pr["head"]["sha"], pr["base"]["sha"], pr["changed_files"],
            pr["state"], pr["draft"], pr["user"]["login"])


def evaluate(get_pr, get_files, get_reviews, expected, repo, *, open_only=True):
    pr = get_pr()
    before = snapshot(pr, expected, repo, open_only=open_only)
    files = get_files()
    needs_review = sensitive(files, pr["changed_files"])
    reviews = get_reviews() if needs_review else []
    verdict = ("PASS" if reviewer_approved(pr, reviews) else "MISSING") if needs_review else "NOT_REQUIRED"
    if snapshot(get_pr(), expected, repo, open_only=open_only) != before:
        raise ValueError("PR changed during review evaluation")
    if needs_review and get_reviews() != reviews:
        raise ValueError("Reviews changed during review evaluation")
    if snapshot(get_pr(), expected, repo, open_only=open_only) != before:
        raise ValueError("PR changed before reporting")
    return verdict


def command(*args):
    return subprocess.check_output(args, text=True)


def api(path):
    return json.loads(command("gh", "api", path))


def pages(path):
    data = json.loads(command("gh", "api", "--paginate", "--slurp", path))
    return [item for page in data for item in page]


def inspect(repo, number, expected, *, open_only=True):
    root = f"repos/{repo}/pulls/{number}"
    return evaluate(lambda: api(root), lambda: pages(root + "/files"),
                    lambda: pages(root + "/reviews"), expected, repo, open_only=open_only)


def status(repo, sha, state, description):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid commit SHA")
    url = f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    command("gh", "api", "--method", "POST", f"repos/{repo}/statuses/{sha}",
            "-f", f"state={state}", "-f", f"context={CONTEXT}",
            "-f", f"description={description}", "-f", f"target_url={url}")


def event_revision(event, payload):
    if event == "pull_request_target":
        pr = payload["pull_request"]
        return int(pr["number"]), pr["head"]["sha"]
    if event == "issue_comment":
        comment = payload["comment"]
        match = re.fullmatch(r"/guardian-recheck ([0-9a-f]{40})", comment["body"].strip())
        if (not match or "pull_request" not in payload["issue"]
                or comment["author_association"] not in ELIGIBLE
                or comment["user"].get("type") != "User"):
            return None
        return int(payload["issue"]["number"]), match.group(1)
    raise ValueError("Untrusted event")


def run_pr(repo, event, payload):
    revision = event_revision(event, payload)
    if revision is None:
        return
    number, sha = revision
    # Validate before writing; an old event must not overwrite the current head.
    pr = api(f"repos/{repo}/pulls/{number}")
    snapshot(pr, sha, repo)
    status(repo, sha, "pending", "Review policy evaluation in progress")
    try:
        verdict = inspect(repo, number, sha)
    except Exception:
        status(repo, sha, "failure", "Review policy could not verify this revision")
        raise
    print(f"Review policy for {sha}: {verdict}")
    if verdict not in {"PASS", "NOT_REQUIRED"}:
        status(repo, sha, "failure", "Sensitive change requires current-head independent review")
        raise SystemExit(1)
    status(repo, sha, "success", f"Review policy: {verdict}")


def audit_push(repo, before, after):
    subprocess.run(["git", "merge-base", "--is-ancestor", before, after], check=True)
    commits = command("git", "rev-list", "--first-parent", f"{before}..{after}").split()
    missing = []
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        print("| Main commit | Pull request | Independent approval |", file=summary)
        print("| --- | --- | --- |", file=summary)
        for sha in commits:
            candidates = pages(f"repos/{repo}/commits/{sha}/pulls")
            merged = next((item for item in candidates
                           if item["merged_at"] and item["merge_commit_sha"] == sha
                           and item["base"]["ref"] == "main"
                           and item["base"]["repo"]["full_name"] == repo), None)
            number = merged["number"] if merged else None
            verdict = inspect(repo, number, merged["head"]["sha"], open_only=False) if merged else "MISSING_PR"
            print(f"| `{sha[:12]}` | {number or '-'} | {verdict} |", file=summary)
            if verdict not in {"PASS", "NOT_REQUIRED"}:
                missing.append(sha)
    if missing:
        raise SystemExit(f"Missing PR provenance or required review for {len(missing)} main commit(s)")


if __name__ == "__main__":
    repo = os.environ["GITHUB_REPOSITORY"]
    event = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        payload = json.load(handle)
    if event == "push":
        audit_push(repo, payload["before"], payload["after"])
    else:
        run_pr(repo, event, payload)
