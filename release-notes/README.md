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
    linear-access-key: ${{ secrets.LINEAR_ACCESS_KEY }}
    openai-api-key:    ${{ secrets.OPENAI_API_KEY }}
    gh-token:          ${{ secrets.GH_TOKEN }}
```

## Setup

`LINEAR_ACCESS_KEY` is a **pipeline access key**, generated per pipeline in Linear under
Settings → Releases → *pipeline* → access key. A personal API key will not work in its
place, and there is no API to generate one. The key identifies the pipeline, so the
action never needs to know which pipeline it is writing to — point each repo at its own.

Turn **off** `Auto-generate release notes on completion` on that pipeline, or Linear's
agent will write a second note alongside this one.

## Notes

- `old-tag` is optional. Left blank, tildactl resolves the previous release tag, skipping
  prereleases and non-version tags (`test_build`, `working_v1.1`, `vtest`). The tag it
  settled on is passed to `linear-release` as `base_ref` so the note and the attached
  issues describe the same commit range.
- Prerelease tags are rejected. Callers should also gate the job so a beta tag push does
  not start a runner — `'v*'` tag filters cannot exclude them on their own.
- The note is uploaded as a build artifact before the Linear call, so a failed publish
  still leaves you the markdown.
- The caller's `actions/checkout` is required and must use `fetch-depth: 0`:
  `linear-release` scans it for issue references. tildactl clones its own copy of
  `repo-name` separately and does not use it.

## Inputs

| Input | Required | Default | |
|---|---|---|---|
| `repo-name` | yes | | Repo tildactl reads, as named under `tildabio` |
| `new-tag` | yes | | Tag being released. Must start with `v`, must not be a prerelease |
| `old-tag` | no | *(resolved)* | Tag to compare from |
| `linear-access-key` | yes | | Linear pipeline access key |
| `openai-api-key` | yes | | Used by tildactl to write the note |
| `gh-token` | yes | | Needs read on `tildabio/tools` and on `repo-name` |
| `bullets` | no | `30` | Total bullets across all sections; `0` is uncapped |
| `release-name-prefix` | no | `Sense` | `Sense v2.10.24` |
| `dry-run` | no | `false` | Write the note, change nothing in Linear |

Outputs: `notes-path`, `old-tag`.
