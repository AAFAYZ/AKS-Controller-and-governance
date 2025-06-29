import logging
import os
import time
import threading
from fastapi import FastAPI, Response, status
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
import uvicorn
from retrying import retry

# Use Azure Workload Identity authentication (no local kube config)
config.load_incluster_config()

# Configurable value inputs from values.yaml (Helm chart)
SLEEP_INTERVAL = int(os.environ.get("SLEEP_INTERVAL", 45))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 20))
RETRY_WAIT_MS = int(os.environ.get("RETRY_WAIT_MS", 2000))
RETRY_MAX_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", 5))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
EXCLUDED_NAMESPACES = os.environ.get("EXCLUDED_NAMESPACES", "").split(",")
DELAY_BETWEEN_RESOURCES = int(os.environ.get("DELAY_BETWEEN_RESOURCES", 3))

# Logging setup
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(message)s")
logger = logging.getLogger("Workload-resource-controller")
EXCLUDED_NAMESPACES: list[str] = [ns.strip() for ns in EXCLUDED_NAMESPACES if ns.strip()]

# Initialize Kubernetes clients
apps_api = client.AppsV1Api()
custom_api = client.CustomObjectsApi()
core_api = client.CoreV1Api()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Intialize FastAPI server
app = FastAPI()

def should_skip_namespace(namespace: str) -> bool:
    """ Determines whether the given namespace is part of the excluded list """
    is_excluded = namespace.strip() in EXCLUDED_NAMESPACES
    if is_excluded:
        logger.info(f"⏭️ Skipping excluded namespace: '{namespace}'")
    return is_excluded

@app.get("/livez")
def health_check() -> dict:
    """ Health check endpoint to verify Kubernetes API responsiveness for the container which is liveness probe using it. """
    try:
        # Check if Kubernetes API is responsive by listing namespaces
        core_api.list_namespace(timeout_seconds=4)
        return {"status": "ok"}
    except Exception as error:
        logger.error(f"❌ /healthz failed: {error}")
        Response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"status": "unhealthy"}

def vpa_body(namespace: str, name: str, kind: str) -> dict:
    """ Constructs the VPA resource manifest. """
    return {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": f"vpa-{name}", "namespace": namespace},
        "spec": {
            "targetRef": {"apiVersion": "apps/v1", "kind": kind, "name": name},
            "updatePolicy": {"updateMode": "Auto"},
            "resourcePolicy": {
                "containerPolicies": [{
                    "containerName": "*",
                    "minAllowed": {"cpu": "5m", "memory": "5Mi"},
                    "maxAllowed": {"cpu": "2", "memory": "4Gi"},
                    "controlledResources": ["cpu", "memory"]
                }]
            }
        }
    }

def scaledobject_body(namespace: str, name: str, kind: str) -> dict:
    """ Constructs the KEDA ScaledObject resource manifest. """
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {"name": f"cpu-scaledobject-{name}", "namespace": namespace},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": kind, "name": name},
            "triggers": [{
                "type": "cpu",
                "metricType": "Utilization",
                "metadata": {"value": "80"}
            }],
            "minReplicaCount": 2,
            "maxReplicaCount": 10
        }
    }

def has_vpa(namespace: str, name: str) -> bool:
    """ Checks whether a VPA already exists for the given workload. """
    vpas = custom_api.list_namespaced_custom_object(
        group="autoscaling.k8s.io", version="v1", namespace=namespace,
        plural="verticalpodautoscalers"
    )
#Iterates over the fetched list of VPA and if any one of the value becomes available in the list, then it returns false
    return any(v["spec"]["targetRef"]["name"] == name for v in vpas.get("items", []))

def has_scaledobject(namespace: str, name: str) -> bool:
    """ Checks whether a ScaledObject already exists for the given workload. """
    sos = custom_api.list_namespaced_custom_object(
        group="keda.sh", version="v1alpha1", namespace=namespace,
        plural="scaledobjects"
    )
#Iterates over the fetched list of scaledobject and if any one of the value becomes available in the list, then it returns false
    return any(iterator["spec"]["scaleTargetRef"]["name"] == name for iterator in sos.get("items", []))

@retry(wait_fixed=RETRY_WAIT_MS, stop_max_attempt_number=RETRY_MAX_ATTEMPTS)
def create_vpa(namespace: str, name: str, kind: str) -> None:
    """ Creates a VerticalPodAutoscaler resource for the workload. """
    custom_api.create_namespaced_custom_object(
        group="autoscaling.k8s.io", version="v1", namespace=namespace,
        plural="verticalpodautoscalers", body=vpa_body(namespace, name, kind)
    )
    logger.info(f"✅ VPA created for {name} in {namespace}")

@retry(wait_fixed=RETRY_WAIT_MS, stop_max_attempt_number=RETRY_MAX_ATTEMPTS)
def create_scaledobject(namespace: str, name: str, kind: str) -> None:
    """ Creates a ScaledObject resource for the workload. """
    custom_api.create_namespaced_custom_object(
        group="keda.sh", version="v1alpha1", namespace=namespace,
        plural="scaledobjects", body=scaledobject_body(namespace, name, kind)
    )
    logger.info(f"✅ ScaledObject created for {name} in {namespace}")

@retry(wait_fixed=RETRY_WAIT_MS, stop_max_attempt_number=RETRY_MAX_ATTEMPTS)
def delete_scaledobject_and_vpa(namespace: str, name: str):
    """ Deletes the VPA and ScaledObject associated with a workload.
        Handles 404 errors which mean the resource does not exist, and skips logging them as errors."""
    try:
        scaledobject_name = f"cpu-scaledobject-{name}"
        custom_api.delete_namespaced_custom_object(
            group="keda.sh", version="v1alpha1", namespace=namespace,
            plural="scaledobjects", name=scaledobject_name
        )
        logger.info(f"🗑️ Deleted ScaledObject: {scaledobject_name} from {namespace}")
    except ApiException as notfound:
        #Log all the errors except 404 status
        if notfound.status != 404:
            logger.error(f"⚠️ Failed to delete ScaledObject {scaledobject_name}: {notfound}")
    try:
        vpa_name = f"vpa-{name}"
        custom_api.delete_namespaced_custom_object(
            group="autoscaling.k8s.io", version="v1", namespace=namespace,
            plural="verticalpodautoscalers", name=vpa_name
        )
        logger.info(f"🗑️ Deleted VPA: {vpa_name} from {namespace}")
    except ApiException as notfound:
        if notfound.status != 404:
            logger.error(f"⚠️ Failed to delete VPA {vpa_name}: {notfound}")

def process_workload(kind: str, namespace: str, name: str) -> None:
    """ Processes an individual workload (Deployment or StatefulSet).
        Creates ScaledObject and VPA if they don't already exist. """
    if should_skip_namespace(namespace):
        return
    logger.info(f"🔍 Processing {kind} {name} in {namespace}")
    try:
        if not has_scaledobject(namespace, name):
            create_scaledobject(namespace, name, kind)
            logger.info(f"⏱ Waiting {DELAY_BETWEEN_RESOURCES}s before creating VPA...")
            time.sleep(DELAY_BETWEEN_RESOURCES)
        if not has_vpa(namespace, name):
            create_vpa(namespace, name, kind)
    except ApiException as error:
        logger.error(f"❌ Error processing {kind} {name}: {error}")

def process_event(kind, obj, event_type) -> None:
    """ Handles a Kubernetes event by checking if the workload still exists and acts accordingly.
        Deletes VPA and Scaledobject only when the workload is truly gone (404 from API). """
    namespace = obj.metadata.namespace
    name = obj.metadata.name
    logger.info(f"Processing {event_type} event for {kind}: {name} in {namespace}")
    if should_skip_namespace(namespace):
        return
    if event_type == "DELETED":
        try:
            # Check if the workload still exists (safety guard)
            if kind == "Deployment":
                apps_api.read_namespaced_deployment(name, namespace)
                logger.info(f"Deployment {name} still exists in {namespace}, skipping ScaledObject/VPA deletion.")
                return
            elif kind == "StatefulSet":
                apps_api.read_namespaced_stateful_set(name, namespace)
                logger.info(f"StatefulSet {name} still exists in {namespace}, skipping ScaledObject/VPA deletion.")
                return
        except ApiException as notfound:
            if notfound.status == 404:
                logger.info(f"{kind} {name} truly deleted. Cleaning up ScaledObject and VPA.")
                delete_scaledobject_and_vpa(namespace, name)
            else:
                logger.error(f"⚠️ Error checking existence of {kind} {name}: {notfound}")
    else:
        process_workload(kind, namespace, name)

def start_watcher(api_func: str, kind: str) -> None:
    """ Continuously watches a resource type and processes events. """
    thread_name = threading.current_thread().name
    logger.info(f"👀 {thread_name} started watching {kind} resources.")
    
    while True:
        try:
            w = watch.Watch()
            for event in w.stream(api_func, timeout_seconds=60):
                obj = event["object"]
                event_type = event["type"]
                process_event(kind, obj, event_type)
        except Exception as error:
            logger.error(f"⚠️ Watch error for {kind}: {error}. Retrying in 5s...")
            time.sleep(5)
            
def main() -> None:
    """ Starts controller threads to watch deployments/statefulsets and runs periodic workload checks. """
    logger.info("🚀 Starting local controller...")
    threads = [
        Thread(target=start_watcher, args=(apps_api.list_deployment_for_all_namespaces, "Deployment"), daemon=True),
        Thread(target=start_watcher, args=(apps_api.list_stateful_set_for_all_namespaces, "StatefulSet"), daemon=True)
    ]
    
    for thread in threads:
        thread.start()
    
    while True:
        time.sleep(SLEEP_INTERVAL)
        logger.info("🔄 Periodic check for workloads...")
        namespaces = [ns.metadata.name for ns in core_api.list_namespace().items]
        for ns in namespaces:
            if should_skip_namespace(ns):
                continue
            for deployment in apps_api.list_namespaced_deployment(ns).items:
                process_workload("Deployment", ns, deployment.metadata.name)
            for statefulset in apps_api.list_namespaced_stateful_set(ns).items:
                process_workload("StatefulSet", ns, statefulset.metadata.name)
             
                
if __name__ == "__main__":
    """ Entrypoint of the script. """
    Thread(target=main, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8080)
