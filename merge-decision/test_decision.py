"""decision.py: how real descriptions read, the mistakes people actually make, and the promise that matters most: the
check and the cherry-pick never disagree."""
import itertools
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import decision as d  # noqa: E402


def status(body, target="production"):
    return d.decide(body, target)["status"]


# -- shapes seen in real PR descriptions (main and cleopatra), and the forgiving variants -----------------------------

@pytest.mark.parametrize("line, want", [
    ("- Merge this PR into branch production: Yes", "valid_yes"),
    ("- Merge this PR into branch production: No", "valid_no"),
    ("Merge this PR into branch production: Yes", "valid_yes"),
    ("* Merge this PR into branch production: No", "valid_no"),
    ("+ Merge this PR into branch production: yes", "valid_yes"),
    ("1. Merge this PR into branch production: YES", "valid_yes"),
    ("- [x] Merge this PR into branch production: Yes", "valid_yes"),
    ("> Merge this PR into branch production: Yes", "valid_yes"),
    ("### Merge this PR into branch production: No", "valid_no"),
    ("- Merge this PR into branch production: **Yes**", "valid_yes"),
    ("- Merge this PR into branch production: **No** — production has its own PR, #16811", "valid_no"),
    ("**Merge this PR into branch production: Yes**", "valid_yes"),
    ("- **Merge this PR into branch production:** Yes", "valid_yes"),
    ("- Merge this PR into branch **production**: _No_", "valid_no"),
    ("- Merge this PR into branch `production`: `Yes`", "valid_yes"),
    ("`Merge this PR into branch production: Yes`", "valid_yes"),                  # the line alone in backticks
    ("- `Merge this PR into branch production: No` ✅", "valid_no"),
    ("- Merge this PR into branch ~~master~~ production: Yes", "valid_yes"),
    ("-   Merge   this  PR\tinto branch   production  :   Yes  ", "valid_yes"),
    ("- Merge this PR into branch production : Yes", "valid_yes"),          # non-breaking spaces
    ("- Merge this PR into branch production：　YES！", "valid_yes"),   # full-width colon, space, bang
    ("- Merge​ this PR into branch production: Yes", "valid_yes"),            # zero-width space
    ("- merge this pr into branch PRODUCTION: yes", "valid_yes"),
    ("- Merge this pull request into branch production: Yes", "valid_yes"),
    ("- Merge this PR into the production branch: Yes", "valid_yes"),
    ("- Merge this PR into production: Yes", "valid_yes"),
    ("- Merge this PR to branch production: No", "valid_no"),
    ("- Merge this PR into branch production - Yes", "valid_yes"),
    ("- Merge this PR into branch production — No", "valid_no"),
    ("- Merge this PR into branch production = yes", "valid_yes"),
    ("- Merge this PR into branch production -> Yes", "valid_yes"),
    ("- Merge this PR into branch production Yes", "valid_yes"),
    ("- Merge this PR into branch production: Yes.", "valid_yes"),
    ("- Merge this PR into branch production: Yes!", "valid_yes"),
    ("- Merge this PR into branch production: Yes, after #123 ships", "valid_yes"),
    ("- Merge this PR into branch production: Yes - hotfix", "valid_yes"),
    ("- Merge this PR into branch production: No (master only), not ready yes", "valid_no"),
    ("- Merge this PR into branch production: Y", "valid_yes"),
    ("- Merge this PR into branch production: n", "valid_no"),
    ("- Merge this PR into branch production: Yes \U0001F680", "valid_yes"),
])
def test_how_a_line_reads(line, want):
    assert status(line) == want


# -- mistakes: each one named, none guessed ----------------------------------------------------------------------------

@pytest.mark.parametrize("line, want", [
    ("- Merge this PR into branch production: **[Replace with Yes or No]**", "placeholder"),
    ("Merge this PR into branch production: Yes/No", "placeholder"),               # cleopatra's template
    ("Merge this PR into branch production: yes or no", "placeholder"),
    ("Merge this PR into branch production: [Yes | No]", "placeholder"),
    ("Merge this PR into branch production: maybe", "unclear"),
    ("Merge this PR into branch production: TBD", "unclear"),
    ("Merge this PR into branch production: yesterday", "unclear"),               # not the word yes
    ("Merge this PR into branch production: nope", "unclear"),
    ("Merge this PR into branch production: yes/maybe", "unclear"),
    ("Merge this PR into branch production: no or later", "unclear"),
    ("Merge this PR into branch production: yesno", "unclear"),
    ("Merge this PR into branch production:", "unclear"),
    ("Merge this PR into branch : No", "no_branch"),
    ("Merge this PR into branch: Yes", "no_branch"),
    ("", "missing"),
    ("Fixes the export. No decision line here.", "missing"),
    ("We should not merge this PR into production before Friday", "missing"),     # prose, not a decision line
    ("`validate-merge-decision` only greps the `Merge this PR into branch <target>: Yes/No` line", "missing"),
    ("erge this PR into branch production: No", "missing"),                       # truncated by a bot (main #16286)
    ("- `Merge this PR into branch production: Yes` on a hotfix PR passes and cherry-picks.", "missing"),  # prose quoting it
    ("> **`AGENTS.md`** says: master PRs -> `Merge this PR into branch production: Yes|No`", "missing"),  # a bot's summary
])
def test_mistakes_are_named(line, want):
    assert status(line) == want


def test_yes_and_no_for_the_same_branch_conflict():
    body = "- Merge this PR into branch production: Yes\n- Merge this PR into branch production: No"
    assert status(body) == "conflicting"


def test_a_filled_line_wins_over_the_template_placeholder_left_elsewhere():
    body = "- Merge this PR into branch production: **[Replace with Yes or No]**\n\nMerge this PR into branch production: No"
    assert status(body) == "valid_no"


def test_the_same_answer_twice_is_fine():
    assert status("Merge this PR into branch production: Yes\n* Merge this PR into branch production: **yes**") == "valid_yes"


def test_html_comments_and_code_blocks_never_count():
    body = "\n".join([
        "<!-- Merge this PR into branch production: Yes -->",
        "<!--", "Example:", "Merge this PR into branch production: Yes", "-->",
        "```", "Merge this PR into branch production: Yes", "```",
        "~~~", "Merge this PR into branch production: No", "~~~",
        "- Merge this PR into branch production: No",
    ])
    assert status(body) == "valid_no"


def test_an_unclosed_comment_hides_the_rest_like_github_renders_it():
    assert status("<!-- notes\nMerge this PR into branch production: Yes") == "missing"


def test_lines_about_other_branches_do_not_count():
    body = "- Merge this PR into branch master: Yes\n- Merge this PR into branch production: No"
    assert status(body, "production") == "valid_no" and status(body, "master") == "valid_yes"


def test_main_and_master_are_one_branch():
    assert status("Merge this PR into branch main: Yes", "master") == "valid_yes"
    assert status("Merge this PR into branch master: No", "main") == "valid_no"


def test_branch_names_with_slashes_and_dots_match_exactly():
    body = "Merge this PR into branch dev/test26: Yes\nMerge this PR into branch release-1.0: Yes"
    assert status(body, "dev/test26") == "valid_yes" and status(body, "release-1.0") == "valid_yes"
    assert status(body, "dev/test27") == "wrong_branch" and status(body, "release-1x0") == "wrong_branch"


def test_a_production_pr_that_decides_about_production_is_told_which_branch_to_name():
    r = d.decide("- Merge this PR into branch production: No", d.target_for("production"))
    assert r["status"] == "wrong_branch" and r["target"] == "master"
    text = d.message(r, "production", "someone")
    assert "@someone" in text and "Merge this PR into branch master: Yes" in text
    assert "> - Merge this PR into branch production: No" in text, "the offending line is quoted"


def test_every_failure_message_names_the_target_and_shows_both_lines():
    for body in ("", "Merge this PR into branch production: maybe", "Merge this PR into branch : No",
                 "Merge this PR into branch production: Yes/No",
                 "Merge this PR into branch production: Yes\nMerge this PR into branch production: No"):
        r = d.decide(body, "production")
        text = d.message(r, "master")
        assert "`production`" in text and "Merge this PR into branch production: Yes" in text \
            and "Merge this PR into branch production: No" in text, (body, text)
    assert d.message(d.decide("Merge this PR into branch production: Yes", "production"), "master") == ""


def test_which_branch_a_pr_must_decide_about():
    assert d.target_for("master") == "production" and d.target_for("main") == "production"
    assert d.target_for("production") == "master"
    assert d.target_for("dev/test26") is None and d.target_for("") is None


# -- the promise: the check and the cherry-pick agree on every description ----------------------------------------

BULLETS = ["", "- ", "* ", "1. ", "- [x] ", "> "]
WRAPS = [("", ""), ("**", "**"), ("_", "_"), ("`", "`")]
SPACES = [" ", "  ", "\t", " "]
SEPS = [":", " :", " -", " —", "", "："]
ANSWERS = ["Yes", "yes", "YES", "No", "no", "Yes/No", "maybe", "[Replace with Yes or No]", "Yes - later", ""]


def variants():
    for bullet, (wl, wr), sp, sep, ans in itertools.product(BULLETS, WRAPS, SPACES, SEPS, ANSWERS):
        yield f"{bullet}Merge{sp}this PR into branch {wl}production{wr}{sep}{sp}{wl}{ans}{wr}"


def test_the_check_and_the_cherry_pick_agree_on_every_variant(tmp_path):
    seen = 0
    for line in variants():
        check = d.decide(line, d.target_for("master"))
        cherry = d.decide(line, "production")
        assert (check["status"] == "valid_yes") == (cherry["status"] == "valid_yes"), line
        ans = line.lower()
        if ans.rstrip("*_` ").endswith(("yes", "yes - later")) and "/no" not in ans and "replace" not in ans:
            assert check["status"] == "valid_yes", line
        seen += 1
    assert seen == len(BULLETS) * len(WRAPS) * len(SPACES) * len(SEPS) * len(ANSWERS)


# -- the command line, as the two actions call it ----------------------------------------------------------------------

def run(args, body, tmp_path, **env):
    out = tmp_path / "github_output"
    out.write_text("")
    r = subprocess.run([sys.executable, os.path.join(HERE, "decision.py"), *args, "--json-out", str(tmp_path / "r.json")],
                       env={**os.environ, "PR_BODY": body, "GITHUB_OUTPUT": str(out), **env},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out.read_text(), json.loads((tmp_path / "r.json").read_text()), r.stdout


def outputs(text):
    """Parse $GITHUB_OUTPUT, including name<<DELIM blocks."""
    vals, lines, i = {}, text.splitlines(), 0
    while i < len(lines):
        if "<<" in lines[i] and "=" not in lines[i].split("<<")[0]:
            key, delim = lines[i].split("<<", 1)
            j = lines.index(delim, i + 1)
            vals[key] = "\n".join(lines[i + 1:j])
            i = j + 1
        else:
            key, _, val = lines[i].partition("=")
            vals[key] = val
            i += 1
    return vals


def test_validate_writes_the_outputs_the_action_reads(tmp_path):
    raw, _, log = run(["validate", "--base", "master", "--head", "fix/x", "--user", "dev"],
                      "- Merge this PR into branch production: **Yes**", tmp_path)
    o = outputs(raw)
    assert o["validation_status"] == "valid_yes" and o["decision"] == "yes" and o["target_branch"] == "production"
    assert o["failed"] == "false" and o["message"] == ""
    assert "found: '- Merge this PR into branch production: **Yes**' -> branch=production, answer=yes" in log


def test_validate_failure_carries_a_multiline_message(tmp_path):
    raw, _, _ = run(["validate", "--base", "production", "--user", "dev"], "- Merge this PR into branch production: No", tmp_path)
    o = outputs(raw)
    assert o["failed"] == "true" and o["validation_status"] == "wrong_branch" and o["target_branch"] == "master"
    assert "Merge this PR into branch master: No" in o["message"] and "\n" in o["message"]


def test_cherry_pick_branches_and_other_bases_are_not_checked(tmp_path):
    o = outputs(run(["validate", "--base", "production", "--head", "cherry-pick-abc123"], "", tmp_path)[0])
    assert o["validation_status"] == "not_applicable" and o["failed"] == "false"
    o = outputs(run(["validate", "--base", "dev/test26"], "", tmp_path)[0])
    assert o["validation_status"] == "not_applicable" and o["failed"] == "false"


def test_should_merge_only_on_an_unambiguous_yes(tmp_path):
    for body, want in [("- Merge this PR into branch production: **Yes**", "true"),
                       ("- Merge this PR into branch production: No", "false"),
                       ("- Merge this PR into branch production: Yes/No", "false"),
                       ("Merge this PR into branch production: Yes\nMerge this PR into branch production: No", "false"),
                       ("<!-- Merge this PR into branch production: Yes -->", "false"),
                       ("", "false")]:
        o = outputs(run(["should-merge", "--target", "production"], body, tmp_path)[0])
        assert o["should_merge"] == want, body


def test_a_hostile_body_is_only_text(tmp_path):
    body = "$(touch /tmp/pwned) `rm -rf /` \"; exit 1\nMerge this PR into branch production: Yes ${HOME}"
    o = outputs(run(["should-merge", "--target", "production"], body, tmp_path)[0])
    assert o["should_merge"] == "true" and not os.path.exists("/tmp/pwned")


def test_both_actions_use_this_parser():
    root = os.path.dirname(HERE)
    for action, cmd in (("validate-merge-decision", "decision.py\" validate"), ("cherry-pick-pr", "decision.py\" should-merge")):
        text = open(os.path.join(root, action, "action.yml")).read()
        assert "$GITHUB_ACTION_PATH/../merge-decision/" + cmd in text, action
