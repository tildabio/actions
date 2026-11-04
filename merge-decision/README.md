# merge-decision

How a pull request says whether it should **also** be merged into another branch, read the same way by:

- [`validate-merge-decision`](../validate-merge-decision): the check on the open PR. It fails until the description
  has one clear answer, and keeps a single comment on the PR that says what to change (then that it is fixed).
- [`cherry-pick-pr`](../cherry-pick-pr): after the merge, cherry-picks the PR into the other branch when the answer is Yes.

Before this, each had its own grep and they disagreed. `**Yes**` failed the check and was ignored by the cherry-pick,
the check reported "placeholder" for any mistake (its pattern matched every line), and its comment lost the branch name
and was re-posted on every edit.

## The line

```
Merge this PR into branch production: Yes
```

| PR into | Decision needed about | Write |
|---|---|---|
| `master` / `main` | `production` | `Merge this PR into branch production: Yes` or `: No` |
| `production` (hotfix) | `master` | `Merge this PR into branch master: Yes` or `: No` |

Yes means the PR is cherry-picked into that branch automatically once merged.

## What is accepted

Formatting does not matter:

- bold, italics or code on any part (`**Yes**`, `` `production` ``), a bullet, number, checkbox, heading or quote in front;
- any spacing, tabs, non-breaking or full-width characters, any case;
- `:`, `-`, `—`, `=`, `->` or nothing before the answer; "pull request" for "PR"; "into the production branch";
- commentary after the answer: `Yes - after #123 ships`, `No (hotfix only)`, `**No** — production has its own PR`;
- `main` and `master` name the same branch;
- ~~struck-through~~ text is dropped.

Ignored completely: anything inside an HTML comment (`<!-- -->`) and fenced code blocks, so template instructions,
examples and pasted logs never count, and a sentence that merely mentions "merge this PR into production".

## What fails, and what the check says

| Status | Example | |
|---|---|---|
| `placeholder` | `... production: **[Replace with Yes or No]**`, `... production: Yes/No` | the template was not filled in |
| `unclear` | `... production: maybe`, `... production: yes/maybe` | the answer is not Yes or No |
| `conflicting` | a Yes line and a No line for the same branch | never guessed |
| `no_branch` | `Merge this PR into branch : No` | |
| `wrong_branch` | a PR into production with `... branch production: No` | names the branch it should be |
| `missing` | no decision line | |

A filled-in line wins over a leftover placeholder; the same answer twice is fine. The cherry-pick only proceeds on
`valid_yes`.

## Changing it

`decision.py` is plain Python 3 (stdlib only). `python -m pytest merge-decision` runs the tests, including a
combinatorial check that the check and the cherry-pick agree on every formatting variant, and
`.github/workflows/merge-decision.yaml` also runs the real check action on the PR. Before merging a change, replay
recent PR descriptions through the old and new code: the action runs from `@main` in every repo the moment it merges.
