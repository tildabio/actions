#!/usr/bin/env python3
"""Publish a tildactl release note onto a Linear release.

One releaseSync call creates (or updates) the release, attaches the issues and pull
requests referenced in the tag range, stores the tag's commit SHA and upserts the note.
Re-running the same tag updates that release rather than adding another: a continuous
pipeline identifies a release by commit SHA, and releaseNotes upserts the note that
covers only that release.
"""
import json, os, re, subprocess, sys, urllib.error, urllib.request

API = "https://api.linear.app/graphql"
# Prefixes that name a Linear issue in this workspace. ENG is almost all of it; the
# others show up in cherry-picks from support and data-migration work.
ISSUE = re.compile(r"\b(?:ENG|SUP|TDM|OPS)-\d+")
PR = re.compile(r"\(#(\d+)\)\s*$")


def auth(key):
    """OAuth tokens are sent as Bearer; personal API keys are sent bare."""
    return f"Bearer {key}" if key.startswith("lin_oauth") else key


def gql(key, query, variables):
    req = urllib.request.Request(
        API, data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": auth(key), "Content-Type": "application/json"})
    try:
        body = json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        sys.exit(f"::error::Linear API HTTP {e.code}: {e.read().decode()[:1000]}")
    if body.get("errors"):
        sys.exit(f"::error::Linear API: {json.dumps(body['errors'])[:1000]}")
    return body["data"]


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=os.environ.get("GITHUB_WORKSPACE") or ".").stdout.strip()


def resolve_pipeline(key, wanted):
    """Find the pipeline by name or slug, so workflows name it instead of carrying a UUID."""
    nodes = gql(key, "{ releasePipelines { nodes { id name slugId type } } }",
                {})["releasePipelines"]["nodes"]
    for p in nodes:
        if wanted.lower() in (p["name"].lower(), p["slugId"].lower()):
            return p
    sys.exit(f"::error::no Linear pipeline named {wanted!r}. "
             f"Found: {', '.join(repr(p['name']) for p in nodes) or 'none'}")


def main():
    key   = os.environ["LINEAR_API_KEY"]
    repo  = os.environ["REPO_NAME"]
    owner = os.environ.get("REPO_OWNER", "tildabio")
    new   = os.environ["NEW_TAG"]
    old   = os.environ.get("OLD_TAG", "")
    notes = open(os.environ["NOTES_PATH"]).read()
    dry   = os.environ.get("DRY_RUN", "false").lower() == "true"

    pipeline = resolve_pipeline(key, os.environ["PIPELINE"])

    rng = f"{old}..{new}" if old else new
    log = git("log", "--format=%H\x1f%s", rng)
    issues, prs, seen = [], [], set()
    for line in filter(None, log.splitlines()):
        sha, subject = line.split("\x1f", 1)
        for ident in ISSUE.findall(subject):
            if (ident, sha) not in seen:
                seen.add((ident, sha))
                issues.append({"identifier": ident, "commitSha": sha})
        m = PR.search(subject)
        if m:
            prs.append({"number": int(m.group(1)),
                        "repositoryOwner": owner, "repositoryName": repo})

    print(f"{rng}: {len(log.splitlines())} commits, "
          f"{len(issues)} issue refs, {len(prs)} PR refs -> pipeline {pipeline['name']!r}")
    if issues:
        print("  " + ", ".join(sorted({i['identifier'] for i in issues})))

    if dry:
        print("dry-run: not writing to Linear")
        return

    data = gql(key, """
      mutation($i: ReleaseSyncInput!) {
        releaseSync(input: $i) {
          success
          release { name version url issueCount stage { name }
                    releaseNotes { url } }
        }
      }""", {"i": {
        "pipelineId": pipeline["id"],
        "commitSha": git("rev-parse", f"{new}^{{commit}}"),
        "version": new,
        "name": f"{os.environ.get('RELEASE_NAME_PREFIX', 'Sense')} {new}".strip(),
        "issueReferences": issues,
        "pullRequestReferences": prs,
        "releaseNotes": {"title": f"{os.environ.get('RELEASE_NAME_PREFIX', 'Sense')} {new}".strip(),
                         "content": notes},
        # provider and url are required alongside owner/name, or the call 400s.
        "repository": {"owner": owner, "name": repo, "provider": "github",
                       "url": f"https://github.com/{owner}/{repo}"},
    }})["releaseSync"]

    rel = data["release"]
    print(f"release: {rel['name']} ({rel['issueCount']} issues, stage {rel['stage']['name']})")
    print(f"  {rel['url']}")
    for n in rel["releaseNotes"]:
        print(f"  note: {n['url']}")

    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(f"\n**Linear:** [{rel['name']}]({rel['url']}) — "
                     f"{rel['issueCount']} issues, stage {rel['stage']['name']}\n")


if __name__ == "__main__":
    main()
