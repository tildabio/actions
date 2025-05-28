# Kaniko Build Action

This action builds Docker images using Kaniko in a Kubernetes environment with self-hosted runners.

## Features

- Builds Docker images using Kaniko executor
- Runs in Kubernetes pods with configurable resources
- Supports git clone from private repositories
- Includes debugging and authentication verification
- Automatic cleanup of build pods
- Configurable timeouts and resource limits
- **Go modules cache using persistent volumes for faster builds (always enabled)**

## Usage

### Basic Usage

```yaml
- name: Build with Kaniko
  uses: tildabio/actions/kaniko-build@main
  with:
    service-name: my-service
    github-token: ${{ secrets.GITHUB_TOKEN }}
```

### Advanced Usage with Custom Go Cache Settings

```yaml
- name: Build with Kaniko
  uses: tildabio/actions/kaniko-build@main
  with:
    service-name: my-service
    service-folder: my-service-folder # If different from service-name
    github-token: ${{ secrets.GITHUB_TOKEN }}
    google-project: my-gcp-project
    artifactory-url: us-central1-docker.pkg.dev
    dockerfile-path: Dockerfile
    namespace: my-namespace
    service-account: my-service-account
    memory-limit: 16Gi
    cpu-limit: 8
    # Go cache configuration (always enabled)
    go-cache-pvc-name: my-go-cache
    go-cache-size: 200Gi
    go-cache-storage-class: fast-ssd
```

## Go Modules Cache

The action **always** creates and manages a persistent volume for Go modules cache, providing significant build speed improvements:

### Benefits:

- **Faster builds**: Avoids re-downloading Go modules across builds
- **Shared cache**: All services use the same cache volume
- **Persistent**: Cache survives pod restarts and deletions
- **Automatic management**: PVC is created automatically if it doesn't exist
- **Always enabled**: No configuration needed, works out of the box

### How it works:

1. Creates a PVC with `ReadWriteMany` access mode (100Gi by default)
2. Mounts the cache volume to `/go-cache` in the Kaniko container
3. Sets `GOCACHE` and `GOMODCACHE` environment variables
4. Go modules and build cache are persisted across builds

### Cache Structure:

```
/go-cache/
├── mod/           # Go modules cache (GOMODCACHE)
└── build-cache/   # Go build cache (GOCACHE)
```

## Inputs

| Input                       | Description                                                    | Required | Default                      |
| --------------------------- | -------------------------------------------------------------- | -------- | ---------------------------- |
| `service-name`              | Service name                                                   | Yes      | -                            |
| `service-folder`            | Service folder name (defaults to service-name if not provided) | No       | -                            |
| `google-project`            | GCP project to use                                             | No       | `tilda-dev-325721`           |
| `artifactory-url`           | Artifactory URL                                                | No       | `us-central1-docker.pkg.dev` |
| `github-token`              | GitHub token for repository access                             | Yes      | -                            |
| `dockerfile-path`           | Path to Dockerfile relative to service folder                  | No       | `Dockerfile`                 |
| `context-path`              | Build context path                                             | No       | `/workspace/main`            |
| `namespace`                 | Kubernetes namespace for Kaniko pod                            | No       | `gh-runners`                 |
| `service-account`           | Kubernetes service account for Kaniko                          | No       | `kaniko-builder`             |
| `timeout-minutes`           | Timeout for the build in minutes                               | No       | `45`                         |
| `memory-request`            | Memory request for Kaniko container                            | No       | `4Gi`                        |
| `memory-limit`              | Memory limit for Kaniko container                              | No       | `12Gi`                       |
| `cpu-request`               | CPU request for Kaniko container                               | No       | `1000m`                      |
| `cpu-limit`                 | CPU limit for Kaniko container                                 | No       | `4`                          |
| `ephemeral-storage-request` | Ephemeral storage request                                      | No       | `10Gi`                       |
| `ephemeral-storage-limit`   | Ephemeral storage limit                                        | No       | `10Gi`                       |
| `go-cache-pvc-name`         | Name of the PVC for Go modules cache                           | No       | `go-modules-cache`           |
| `go-cache-size`             | Size of the Go modules cache PVC                               | No       | `100Gi`                      |
| `go-cache-storage-class`    | Storage class for Go modules cache PVC                         | No       | `premium-rwx`                |

## Outputs

| Output      | Description     |
| ----------- | --------------- |
| `image-tag` | Built image tag |
| `image-url` | Full image URL  |

## Storage Class Recommendations

For **Google Kubernetes Engine (GKE)**, recommended storage classes for optimal performance:

### **Recommended (Default):**

- **`premium-rwx`** - Best balance of performance and cost for Go cache
- Fast Google Cloud Filestore with ReadWriteMany support
- Optimized for concurrent access patterns

### **High Performance Options:**

- **`enterprise-multishare-rwx`** - Enterprise-grade performance for heavy workloads
- **`enterprise-rwx`** - Enterprise performance tier
- **`parallelstore-rwx`** - Extreme performance (likely overkill for Go cache)

### **Budget Options:**

- **`standard-rwx`** - Lower cost, adequate performance
- **`zonal-rwx`** - Zone-specific, cost-effective

### **Not Suitable:**

- `standard-rwo`, `premium-rwo` - ReadWriteOnce only (single pod access)
- `standard` - Legacy, limited functionality

## Requirements

- Self-hosted runners with Kubernetes access
- Configured service account with appropriate permissions
- Access to Google Artifact Registry
- kubectl configured in the runner environment
- **Storage class that supports `ReadWriteMany` access mode for Go cache**

## Example Workflow

```yaml
name: Build with Kaniko

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: arc-runner-scaleset
    timeout-minutes: 45
    steps:
      - name: Build with Kaniko
        uses: tildabio/actions/kaniko-build@main
        with:
          service-name: my-service
          github-token: ${{ secrets.GITHUB_TOKEN }}
          # Go cache is always enabled automatically!
```

## Performance Optimization

### First Build vs Subsequent Builds

**First build** (cold cache):

- Downloads all Go modules
- Populates the cache
- Normal build time

**Subsequent builds** (warm cache):

- Reuses cached modules
- **Significantly faster build times**
- Only downloads new/updated dependencies

### Expected Performance Gains:

- **50-80% faster** Go dependency resolution
- **20-40% overall build time reduction**
- Scales better with larger dependency trees

## Migration from ojas-test.yaml

To migrate from the original Kaniko implementation in `ojas-test.yaml`:

1. Replace the entire build step with the action call
2. Change `runs-on` to use self-hosted runners (`arc-runner-scaleset`)
3. Pass required inputs (`service-name`, `github-token`)
4. **Go cache is automatically enabled** - no configuration needed
5. Optionally customize cache settings (PVC name, size, storage class)

### Before (ojas-test.yaml)

```yaml
jobs:
  build-authz:
    runs-on: arc-runner-scaleset
    timeout-minutes: 45
    steps:
      - name: Set image tag
        # ... complex setup logic
      - name: Build with Kaniko
        # ... 200+ lines of Kaniko pod configuration
```

### After (using this action with automatic Go cache)

```yaml
jobs:
  build-authz:
    runs-on: arc-runner-scaleset
    timeout-minutes: 45
    steps:
      - name: Build with Kaniko
        uses: tildabio/actions/kaniko-build@main
        with:
          service-name: authz
          github-token: ${{ secrets.GITHUB_TOKEN }}
          # Go cache automatically enabled!
```

## Troubleshooting

### Go Cache Issues

1. **PVC not binding**: Check if your storage class supports `ReadWriteMany`
2. **Permission errors**: The action sets `chmod 777` on cache directories
3. **Cache not working**: Verify `GOCACHE` and `GOMODCACHE` environment variables are set
4. **Storage full**: Monitor cache usage and increase `go-cache-size` if needed

### Checking Cache Usage

The action automatically reports cache usage after successful builds. You can also manually check:

```bash
kubectl exec -it <pod-name> -n gh-runners -c kaniko -- du -sh /go-cache/
```
