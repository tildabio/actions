# release-notes

Generates a release note with `tildactl` and publishes it onto a release in a Linear
release pipeline, in one step.

```yaml
- uses: actions/checkout@v4
  with: { ref: ${{ github.ref_name }}, fetch-depth: 0 }

- uses: tildabio/actions/release-notes@main
  with:
    repo-name: main                 # as named under tildabio
    new-tag: ${{ github.ref_name }} # v2.10.24
    pipeline: Sense Backend         # Linear pipeline, by name or slug
    linear-api-key: ${{ secrets.LINEAR_API_KEY }}
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
    gh-token:       ${{ secrets.GH_TOKEN }}
```

## Setup

- **`LINEAR_API_KEY`** — a Linear API key or OAuth token, set org-wide. *Not* a pipeline
  access key: this action talks to the GraphQL API directly, so nothing has to be
  generated per pipeline.
- **`pipeline`** is resolved by name or slug at run time, so the workflow carries a
  readable name rather than a UUID and renaming a pipeline is a one-line change.
- Turn **off** `Auto-generate release notes on completion` on the pipeline, or Linear's
  agent writes a second note alongside this one.

## Behaviour

One `releaseSync` call creates the release, attaches every issue and pull request
referenced in the tag range, stores the tag's commit SHA and upserts the note. Re-running
the same tag **updates** that release rather than creating another — a continuous pipeline
identifies a release by commit SHA, and the note upsert covers only that release. So a
failed run is safe to retry, and a manual re-run refreshes an existing release.

Issue references are read from commit subjects (`ENG-`, `SUP-`, `TDM-`, `OPS-`) over the
same range the note describes, and PR numbers from the trailing `(#12345)`.

## Notes

- `old-tag` is optional. Left blank, tildactl resolves the previous release tag, skipping
  prereleases and non-version tags (`test_build`, `working_v1.1`, `vtest`). Whatever it
  settles on is what the issue scan uses, so the note and the attached issues always
  describe the same commits.
- Prerelease tags are rejected. Callers should also gate the job so a beta tag push does
  not start a runner — `'v*'` tag filters cannot exclude them on their own.
- The note is uploaded as a build artifact before the Linear call, so a failed publish
  still leaves you the markdown.
- The caller's `actions/checkout` is required and must use `fetch-depth: 0`: the issue
  scan reads it. tildactl clones its own copy of `repo-name` separately.

## Inputs

| Input | Required | Default | |
|---|---|---|---|
| `repo-name` | yes | | Repo tildactl reads, as named under `tildabio` |
| `new-tag` | yes | | Tag being released. Must start with `v`, must not be a prerelease |
| `pipeline` | yes | | Linear pipeline, by name or slug |
| `linear-api-key` | yes | | Linear API key or OAuth token |
| `openai-api-key` | yes | | Used by tildactl to write the note |
| `gh-token` | yes | | Needs read on `tildabio/tools` and on `repo-name` |
| `old-tag` | no | *(resolved)* | Tag to compare from |
| `bullets` | no | `30` | Total bullets across all sections; `0` is uncapped |
| `release-name-prefix` | no | `Sense` | `Sense v2.10.24` |
| `dry-run` | no | `false` | Write the note, change nothing in Linear |

Outputs: `notes-path`, `old-tag`.
