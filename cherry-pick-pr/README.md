# Cherry Pick PR Action

A reusable GitHub Action that automatically cherry-picks merged PRs to a target branch based on a decision pattern in the PR body.

## Features

- ✅ Automatic cherry-pick based on PR body decision
- ✅ Works with any branch names (master/main/production/staging/develop)
- ✅ Configurable decision patterns
- ✅ Customizable labels and PR body
- ✅ Safe validation before cherry-picking
- ✅ Works across any repository

## Usage

### Basic Example

```yaml
name: Cherry Pick to Production

on:
  pull_request:
    types: [closed]
    branches: [master]

jobs:
  cherry-pick:
    name: Cherry pick to production
    runs-on: ubuntu-latest
    if: github.event.pull_request.merged == true

    steps:
      - name: Cherry pick to production branch
        uses: tildabio/actions/cherry-pick-pr@main
        with:
          source-branch: 'master'
          target-branch: 'production'
          github-token: ${{ secrets.GH_PAT }}
          pr-body: ${{ github.event.pull_request.body }}
```

### Advanced Example with Custom Configuration

```yaml
- name: Cherry pick to staging
  uses: tildabio/actions/cherry-pick-pr@main
  with:
    source-branch: 'develop'
    target-branch: 'staging'
    github-token: ${{ secrets.GH_PAT }}
    pr-body: ${{ github.event.pull_request.body }}
    cherry-pick-branch-prefix: 'auto-merge-'
    cherry-pick-pr-body: |
      Automated cherry-pick from develop to staging
      [x] cleanup branches
    labels: |
      cherry-pick
      automated
      staging
```

## Inputs

| Input | Description | Required | Default |
|-------|-------------|----------|---------|
| `source-branch` | Branch where PR was merged (e.g., master, main, develop) | Yes | - |
| `target-branch` | Branch to cherry-pick into (e.g., production, staging) | Yes | - |
| `github-token` | GitHub token for API operations (use `secrets.GH_PAT` or `secrets.GITHUB_TOKEN`) | Yes | - |
| `pr-body` | The PR body text to check for merge decision | Yes | - |
| `cherry-pick-branch-prefix` | Prefix for the cherry-pick branch name | No | `cherry-pick-` |
| `cherry-pick-pr-body` | Body text for the created cherry-pick PR | No | `[x] cleanup branches` |
| `labels` | Labels to add to cherry-pick PR (newline or comma separated) | No | `cherry-pick`<br>`automerge` |
| `decision-pattern` | Custom regex pattern to search in PR body | No | Auto-generated |

## Outputs

| Output | Description |
|--------|-------------|
| `should-cherry-pick` | Whether the cherry-pick should proceed (`true`/`false`) |
| `cherry-pick-status` | Status of the operation (`success`/`skipped`/`failed`) |

## How It Works

1. **Decision Check**: The action looks for a decision pattern in the PR body
   - Default pattern: `Merge this PR into branch {target-branch}: Yes`
   - Case insensitive, extra spaces are okay

2. **Cherry Pick**: If approved, creates a new PR with the changes cherry-picked to the target branch

3. **Auto-merge**: Applies configurable labels (e.g., `automerge`) for automatic merging

## PR Body Format

Add this line to your PR description to trigger cherry-picking:

```
Merge this PR into branch production: Yes
```

Or for other branches:
```
Merge this PR into branch staging: Yes
Merge this PR into branch main: Yes
```

### Flexible Matching

The action is very forgiving and handles common typos/variations:

✅ **Case insensitive**:
- `Merge this PR into branch production: Yes`
- `MERGE THIS PR INTO BRANCH PRODUCTION: YES`
- `merge this pr into branch production: yes`

✅ **Extra spaces**:
- `Merge  this  PR  into  branch  production : Yes` (multiple spaces)
- `Merge this PR into branch production :Yes` (no space after colon)

✅ **Extra text after "yes"**:
- `Merge this PR into branch production: Yes!!!`
- `Merge this PR into branch production: Yes.`
- `Merge this PR into branch production: Yes\n\nSome other description...`

❌ **Won't match** (correctly rejected):
- `Merge this PR into branch production: yes/no` (ambiguous - has slash)
- `Merge this PR into branch production: yesno` (concatenated - no word boundary)
- `Merge this PR into branch production: yesterday` (false positive prevented)
- `Merge this PR into branch production: No`
- `Merge this PR into branch production: Maybe`
- `Merge this PR into branch release-v1x0: Yes` (typo in branch name)

## Technical Details

### Pattern Matching Features

The action uses advanced regex with:

1. **Word Boundaries** (`\<yes\>`) - Prevents false positives from words containing "yes"
2. **Regex Escaping** - Handles branch names with special characters (dots, slashes, etc.)
3. **Slash Detection** - Explicitly rejects "yes/no" or "yes/" patterns
4. **Flexible Whitespace** - Handles tabs, newlines, multiple spaces
5. **Case Insensitive** - Accepts any case combination

### Edge Cases Handled

| Input | Result | Reason |
|-------|--------|--------|
| `yes` | ✅ Accept | Valid decision |
| `YES!!!` | ✅ Accept | Punctuation allowed |
| `Yes, after review` | ✅ Accept | Additional text allowed |
| `yesterday` | ❌ Reject | Word boundary check |
| `yesno` | ❌ Reject | Word boundary check |
| `yes/no` | ❌ Reject | Explicit slash detection |
| `yes/maybe` | ❌ Reject | Explicit slash detection |

### Branch Name Support

Works with branches containing special characters:
- `production`, `staging`, `develop` ✅
- `release/v1.0`, `hotfix/urgent` ✅
- `feature-branch`, `bug_fix` ✅
- Prevents typos: `release-v1x0` won't match `release-v1.0` ✅

## Complete Workflow Example

```yaml
name: Bidirectional Cherry Pick

on:
  pull_request:
    types: [closed]
    branches: [master, production]

jobs:
  cherry-pick-to-production:
    name: Cherry pick to production
    runs-on: ubuntu-latest
    if: |
      github.event.pull_request.merged == true &&
      github.event.pull_request.base.ref == 'master'

    steps:
      - name: Cherry pick to production
        id: cherry-pick
        uses: tildabio/actions/cherry-pick-pr@main
        with:
          source-branch: 'master'
          target-branch: 'production'
          github-token: ${{ secrets.GH_PAT }}
          pr-body: ${{ github.event.pull_request.body }}

  cherry-pick-to-master:
    name: Cherry pick to master
    runs-on: ubuntu-latest
    if: |
      github.event.pull_request.merged == true &&
      github.event.pull_request.base.ref == 'production'

    steps:
      - name: Cherry pick to master
        id: cherry-pick
        uses: tildabio/actions/cherry-pick-pr@main
        with:
          source-branch: 'production'
          target-branch: 'master'
          github-token: ${{ secrets.GH_PAT }}
          pr-body: ${{ github.event.pull_request.body }}

  notify-on-failure:
    name: Notify on failure
    runs-on: ubuntu-latest
    if: always()
    needs: [cherry-pick-to-production, cherry-pick-to-master]

    steps:
      - name: Send notification
        if: |
          needs.cherry-pick-to-production.result == 'failure' ||
          needs.cherry-pick-to-master.result == 'failure'
        uses: tildabio/actions/gchat-notify@kaniko-builds
        with:
          user-to-mention: ${{ github.event.pull_request.user.login }}
          job-results: |
            {
              "cherry-pick": "${{ needs.cherry-pick-to-production.result || needs.cherry-pick-to-master.result }}"
            }
```

## License

MIT
