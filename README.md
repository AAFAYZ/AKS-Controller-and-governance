# Workload Resource Controller

A Kubernetes controller that automates the creation and management of KEDA ScaledObjects and Vertical Pod Autoscalers (VPA) for Deployments and StatefulSets across all namespaces, excluding specified namespaces.

## 🚀 Features

- **Automated ScaledObject and VPA Creation**: Monitors Deployments and StatefulSets to ensure corresponding ScaledObjects and VPAs are present.
- **ResourceQuota Enforcement**: Ensures each namespace has a default ResourceQuota.
- **Namespace Exclusion**: Skips specified namespaces based on environment configuration.
- **Pattern based Namespace Exclusion**: Skips wildcard based patterns in the namespaces.
- **Health Check Endpoint**: Provides a `/livez` endpoint for liveness probes.
- **Concurrent Processing**: Utilizes threading for efficient resource monitoring and management.
- **Retry Mechanism**: Implements retries for resource creation and deletion to handle transient errors.
- **Annotation based skipping**: When the annotation is provided in the metadata, the controller will skip attaching the resources

## ⚙️ Configuration

The controller can be configured via environment variables:

- `SLEEP_INTERVAL`: Interval in seconds between periodic checks. Default is `45`.
- `MAX_WORKERS`: Maximum number of worker threads. Default is `20`.
- `RETRY_WAIT_MS`: Wait time in milliseconds between retries. Default is `2000`.
- `RETRY_MAX_ATTEMPTS`: Maximum number of retry attempts. Default is `5`.
- `LOG_LEVEL`: Logging level (e.g., `INFO`, `DEBUG`). Default is `INFO`.
- `EXCLUDED_NAMESPACES`: Comma-separated list of namespaces to exclude.
- `DELAY_BETWEEN_RESOURCES`: Delay in seconds between creating ScaledObject and VPA. Default is `3`.

## 🛠️ Deployment

This controller is designed to run within a Kubernetes cluster. It can be deployed using a Helm chart with configurable `values.yaml`.

## 🩺 Health Check

The controller exposes a liveness probe at `/livez` to verify Kubernetes API responsiveness.