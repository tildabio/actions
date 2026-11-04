"""Reads the merge decision in a pull request description: whether the PR should also be merged into another branch.

Both validate-merge-decision (the check on the open PR) and cherry-pick-pr (which acts on the decision once the PR is
merged) run this file, so the check and the cherry-pick can never disagree about what a description says. Before, each
had its own grep: `**Yes**` failed one and was ignored by the other, and a commented-out example could trigger a
cherry-pick.

The line, one per description:

    Merge this PR into branch production: Yes
    Merge this PR into branch master: No

Forgiving about how it is written:
  * Markdown around any part: **bold**, *italic*, _italic_, `code`, a list bullet (- * + 1.), a checkbox, a heading
    or a quote marker in front. ~~Struck-through~~ text is dropped: it was withdrawn.
  * Any spacing, tabs or non-breaking spaces; full-width punctuation; any case.
  * ":" or "-", "=", "->", an en/em dash, or nothing between the branch and the answer; "branch" before or after the
    branch name, or not at all; "pull request" for "PR".
  * Commentary after the answer: "Yes - after #123 ships", "No (hotfix only)", "**No** — production has its own PR".
  * Anything inside an HTML comment (<!-- -->) or a fenced code block is ignored, so instructions, examples and
    pasted text never count.
  * main and master name the same branch.

Strict about what it says:
  * The answer is Yes or No (Y or N). "Yes/No", "Yes or No" and "[Replace with Yes or No]" are the unfilled template.
  * One answer: a Yes and a No for the same branch is a conflict, never a guess.
  * The line has to start with "Merge this PR into": a sentence that merely mentions one is not a decision.
  * A line about the wrong branch (a PR into production deciding about production) is called out by name.

    python3 decision.py validate --base <base branch> [--head <head branch>]   (the check)
    python3 decision.py should-merge --target <branch>                          (the cherry-pick)

The description is read from $PR_BODY. Results go to $GITHUB_OUTPUT when set, and as JSON to stdout.
"""
import argparse
import json
import os
import re
import sys
import unicodedata

DEFAULT_BRANCHES = {"master", "main"}  # the same branch under two names, depending on the repo
COMMENT = re.compile(r"<!--.*?(-->|$)", re.S)
ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
PREFIX = re.compile(r"^(?:\s*(?:>+|#{1,6}|[-*+•]|\d+[.)]|\[[ xX]?\])\s*)+")
STRUCK = re.compile(r"~~.*?~~")  # struck-through text is withdrawn: "~~master~~ production" means production
EMPHASIS = re.compile(r"\*+|`+|~~|(?<![\w])_+|_+(?![\w])")
LINE = re.compile(
    r"^merge\s+this\s+(?:pr|pull\s*request)\s+(?:in)?to\s+(?:the\s+)?(?:branch\s+)?"
    r"(?P<branch>[a-z0-9][\w./-]*)?(?:\s+branch)?"
    r"\s*(?P<sep>:|=|->|-+|–|—)?\s*(?P<value>.*)$",
    re.I)
COMMENTARY = re.compile(r"\s[-–—]\s|[–—(,;.!?]|\s-{2,}")
YES, NO = {"yes", "y"}, {"no", "n"}


class Line:
    """One "Merge this PR into ..." line: what was written, which branch it names, and how its answer reads."""

    def __init__(self, raw, branch, answer):
        self.raw, self.branch, self.answer = raw, branch, answer  # answer: yes, no, placeholder, unclear, missing

    def to_dict(self):
        return {"line": self.raw, "branch": self.branch, "answer": self.answer}


def plain(line):
    """The line as text: no markdown decoration, normalised characters, single spaces."""
    line = unicodedata.normalize("NFKC", line)
    line = ZERO_WIDTH.sub("", line)
    line = PREFIX.sub("", line)
    line = STRUCK.sub("", line)
    line = EMPHASIS.sub("", line)
    line = PREFIX.sub("", line)  # a bullet inside bold, "**- Merge ...**"
    return re.sub(r"\s+", " ", line).strip()


def answer_of(value):
    value = value.strip().lower()
    if not value:
        return "missing"
    if "replace with" in value:
        return "placeholder"
    head = COMMENTARY.split(value, 1)[0]
    words = re.findall(r"[a-z]+", head)
    if not words:
        return "missing"
    has_yes, has_no = any(w in YES for w in words), any(w in NO for w in words)
    if has_yes and has_no:
        return "placeholder"  # "Yes/No", "yes or no", "[Yes | No]": the choice, not an answer
    if re.search(r"\b(?:yes|y|no|n)\s*(?:/|\||\bor\b)", head):
        return "unclear"  # "yes/maybe", "no or later": hedged, not an answer
    if words[0] in YES:
        return "yes"
    if words[0] in NO:
        return "no"
    return "unclear"


FENCE = re.compile(r"^\s*(?:>\s*)*(```|~~~)")


def lines_in(body):
    found, fenced = [], False
    for raw in COMMENT.sub("", body or "").splitlines():
        if FENCE.match(raw):
            fenced = not fenced  # a code block quotes text (examples, logs); it never holds the decision
            continue
        if fenced:
            continue
        m = LINE.match(plain(raw))
        if m:
            branch = (m.group("branch") or "").lower().rstrip("-./")
            if branch in ("branch", "the"):  # "into branch: Yes" names no branch
                branch = ""
            found.append(Line(raw.strip(), branch, answer_of(m.group("value") or "")))
    return found


def same_branch(a, b):
    a, b = a.lower(), b.lower()
    return a == b or (a in DEFAULT_BRANCHES and b in DEFAULT_BRANCHES)


def decide(body, target):
    """The decision about `target`. status: valid_yes, valid_no, conflicting, placeholder, unclear, no_branch,
    wrong_branch or missing; decision: yes, no or none."""
    every = lines_in(body)
    mine = [l for l in every if l.branch and same_branch(l.branch, target)]
    answers = {l.answer for l in mine}
    result = {"target": target, "lines": [l.to_dict() for l in mine], "other_lines": [l.to_dict() for l in every if l not in mine]}
    if {"yes", "no"} <= answers:
        return {**result, "status": "conflicting", "decision": "none"}
    if "yes" in answers:
        return {**result, "status": "valid_yes", "decision": "yes"}
    if "no" in answers:
        return {**result, "status": "valid_no", "decision": "no"}
    if "placeholder" in answers:
        return {**result, "status": "placeholder", "decision": "none"}
    if mine:
        return {**result, "status": "unclear", "decision": "none"}
    if any(not l.branch for l in every):
        return {**result, "status": "no_branch", "decision": "none"}
    if every:
        return {**result, "status": "wrong_branch", "decision": "none"}
    return {**result, "status": "missing", "decision": "none"}


def target_for(base):
    """The branch a PR into `base` has to decide about, or None when the check does not apply."""
    base = (base or "").lower()
    if base in DEFAULT_BRANCHES:
        return "production"
    if base == "production":
        return "master"
    return None


def message(result, base, user=""):
    """What to tell the author, in markdown. Empty when the decision is fine."""
    status, target = result["status"], result["target"]
    if status in ("valid_yes", "valid_no", "not_applicable"):
        return ""
    if status == "conflicting" or not result["lines"]:
        quoted = result["lines"] or result["other_lines"]
    else:
        quoted = result["lines"]
    shown = "\n".join(f"> {l['line']}" for l in quoted[:4])
    why = {
        "conflicting": f"It says both **Yes** and **No** for `{target}`:",
        "placeholder": "The template's placeholder is still there:",
        "unclear": f"The answer for `{target}` is not Yes or No:",
        "no_branch": "This line does not name a branch:",
        "wrong_branch": (f"This PR goes into `{base}`, so the question is whether it should **also** go into `{target}`. "
                         f"The line below decides about a different branch:"),
        "missing": f"There is no line saying whether this PR should also go into `{target}`.",
    }[status]
    hello = f"@{user} " if user else ""
    parts = [f"### Merge decision needed: `{target}`", "", f"{hello}{why}"]
    if shown:
        parts += ["", shown]
    parts += ["", "Put exactly one of these lines in the description (the check re-runs when you save it):", "",
              "```", f"Merge this PR into branch {target}: Yes", f"Merge this PR into branch {target}: No", "```", "",
              "<sub>Bold, bullets, spacing and case do not matter; the answer must be Yes or No. "
              "Yes means the PR is cherry-picked into "
              f"`{target}` automatically once it is merged.</sub>"]
    return "\n".join(parts)


def write_outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            value = str(value)
            if "\n" in value:
                delim = "EOF_" + os.urandom(8).hex()
                f.write(f"{key}<<{delim}\n{value}\n{delim}\n")
            else:
                f.write(f"{key}={value}\n")


def report(r, base=None):
    """What the run log shows: every decision line found and how it was read."""
    if base is not None:
        print(f"PR into {base or '?'}: decision needed about {r['target'] or '-'}")
    for l in r["lines"] + r["other_lines"]:
        print(f"  found: {l['line']!r} -> branch={l['branch'] or '?'}, answer={l['answer']}")
    print(f"Result: {r['status']}" + (f" ({r['reason']})" if r.get("reason") else ""))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="the check on an open PR")
    v.add_argument("--base", required=True)
    v.add_argument("--head", default="")
    v.add_argument("--user", default="")
    s = sub.add_parser("should-merge", help="the cherry-pick: is the answer for --target Yes?")
    s.add_argument("--target", required=True)
    for x in (v, s):
        x.add_argument("--json-out", default="", help="also write the full result as JSON to this file")
    a = p.parse_args(argv)
    body = os.environ.get("PR_BODY", "")

    if a.cmd == "should-merge":
        r = decide(body, a.target)
        should = r["status"] == "valid_yes"
        write_outputs({"should_merge": "true" if should else "false", "status": r["status"]})
        report(r)
        print(f"Cherry-pick into {a.target}: {'yes' if should else 'no'}")
        dump(a.json_out, {**r, "should_merge": should})
        return 0

    target = target_for(a.base)
    if a.head.lower().startswith("cherry-pick"):
        r = {"status": "not_applicable", "decision": "none", "target": target or "", "lines": [], "other_lines": [],
             "reason": f"cherry-pick branch {a.head}: the decision was made on the original PR"}
    elif target is None:
        r = {"status": "not_applicable", "decision": "none", "target": "", "lines": [], "other_lines": [],
             "reason": f"no merge decision is asked for PRs into {a.base}"}
    else:
        r = decide(body, target)
    text = message(r, a.base, a.user)
    write_outputs({"validation_status": r["status"], "decision": r["decision"], "target_branch": r["target"],
                   "failed": "true" if text else "false", "message": text})
    report(r, a.base)
    dump(a.json_out, {**r, "message": text})
    return 0


def dump(path, data):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)


if __name__ == "__main__":
    sys.exit(main())
