import os
import asyncio
import fnmatch
import kopf
import threading
import uvicorn
import logging
import yaml
import pytz
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import kubernetes
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.client import ApiClient

# -------------------------------------------------------------------
#  Load Kubernetes configuration
# -------------------------------------------------------------------
config.load_incluster_config()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(message)s")
logger = logging.getLogger("Workload-resource-controller")
last_cleanup_date = None  # To track last cleanup run date

apps_api = client.AppsV1Api()
custom_api = client.CustomObjectsApi()
core_api = client.CoreV1Api()
api_client = ApiClient()

# Default delay in minutes which is overridden by annotation)
DEFAULT_DELAY_MINUTES = int(os.getenv("DEFAULT_DELAY_MINUTES", "0"))

# Namespace exclusions & glob patterns
EXCLUDED_NAMESPACES = os.getenv("EXCLUDED_NAMESPACES", "").split(",")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
MAX_DELAY_MINUTES = int(os.getenv("MAX_DELAY_MINUTES", "60"))  # maximum allowed delay
CONFIG_MAP_PATH = "/app/config/workload-resource-controller-cm.yaml"

# -------------------------------------------------------------------
#  Defaults (will be overwritten by ConfigMap if exists)
# -------------------------------------------------------------------
DEFAULT_SCALEDOBJECT = {
"minReplicaCount": int(os.getenv("SCALEDOBJECT_MIN_REPLICAS", "2")),
"maxReplicaCount": int(os.getenv("SCALEDOBJECT_MAX_REPLICAS", "10")),
"cpuTriggerValue": int(os.getenv("SCALEDOBJECT_CPU_TRIGGER", "80")),
}

DEFAULT_VPA = {
"updateMode": os.getenv("VPA_UPDATE_MODE", "Auto"),
"minAllowed": {
    "cpu": os.getenv("VPA_MIN_CPU", "10m"),
    "memory": os.getenv("VPA_MIN_MEM", "64Mi"),
},
"maxAllowed": {
    "cpu": os.getenv("VPA_MAX_CPU", "2"),
    "memory": os.getenv("VPA_MAX_MEM", "2Gi"),
},
}

# -------------------------------------------------------------------
#  Load defaults from ConfigMap
# -------------------------------------------------------------------
def load_configmap_defaults(namespace="", name="workload-controller-cm"):
    """Load ScaledObject and VPA defaults from ConfigMap"""
    global DEFAULT_SCALEDOBJECT, DEFAULT_VPA

    if os.path.exists(CONFIG_MAP_PATH):
        try:
            with open(CONFIG_MAP_PATH, "r") as f:
                config_yaml = yaml.safe_load(f) or {}

            # Update ScaledObject to use configmap values
            so_config = config_yaml.get("scaledObject", {})
            DEFAULT_SCALEDOBJECT.update({
                "minReplicaCount": int(so_config.get("minReplicaCount", DEFAULT_SCALEDOBJECT["minReplicaCount"])),
                "maxReplicaCount": int(so_config.get("maxReplicaCount", DEFAULT_SCALEDOBJECT["maxReplicaCount"])),
                "cpuTriggerValue": int(so_config.get("cpuTriggerValue", DEFAULT_SCALEDOBJECT["cpuTriggerValue"])),
            })

            # Update VPA to use configmap values
            vpa_config = config_yaml.get("vpa", {})
            DEFAULT_VPA.update({
                "updateMode": vpa_config.get("updateMode", DEFAULT_VPA["updateMode"]),
                "minAllowed": {
                    "cpu": vpa_config.get("minAllowed", {}).get("cpu", DEFAULT_VPA["minAllowed"]["cpu"]),
                    "memory": vpa_config.get("minAllowed", {}).get("memory", DEFAULT_VPA["minAllowed"]["memory"]),
                },
                "maxAllowed": {
                    "cpu": vpa_config.get("maxAllowed", {}).get("cpu", DEFAULT_VPA["maxAllowed"]["cpu"]),
                    "memory": vpa_config.get("maxAllowed", {}).get("memory", DEFAULT_VPA["maxAllowed"]["memory"]),
                },
            })

            logger.info(f"✅ Loaded ConfigMap values from {CONFIG_MAP_PATH}")

        except Exception as e:
            logger.warning(f"⚠️ Failed to load ConfigMap {CONFIG_MAP_PATH}: {e}")
    else:
        logger.info(f"ℹ️ ConfigMap file {CONFIG_MAP_PATH} not found, using defaults")

# Load ConfigMap at startup
load_configmap_defaults()

# -------------------------------------------------------------------
#  Livesness probe via FastAPI
# -------------------------------------------------------------------

app = FastAPI(title="Workload Controller Health")

@app.get("/livez")
async def liveness():
    """Liveness probe: check if controller process is running"""
    return JSONResponse(content={"status": "ok"})

# -------------------------------------------------------------------
#  Utility functions
# -------------------------------------------------------------------
def is_namespace_excluded(namespace: str) -> bool:
    """Check namespace exclusion"""
    for pattern in EXCLUDED_NAMESPACES:
        if fnmatch.fnmatch(namespace.strip().lower(), pattern.lower()):
            logger.info(f"⏭️ Skipping excluded namespace: '{namespace}'")
            return True
    return False

# -------------------------------------------------------------------
#  Utility: Check workload readiness
# -------------------------------------------------------------------
def workload_fully_ready(kind: str, obj) -> bool:
    """Return True if desired replicas == available/ready replicas"""
    if kind == "Deployment":
        desired = obj.spec.replicas or 0
        available = obj.status.available_replicas or 0
        return desired == available and desired > 0
    elif kind == "StatefulSet":
        desired = obj.spec.replicas or 0
        ready = obj.status.ready_replicas or 0
        return desired == ready and desired > 0
    return False


# -------------------------------------------------------------------
#  Helper to remove finalizers
# -------------------------------------------------------------------
async def remove_finalizers_safe(namespace: str, name: str, kind: str, is_new: bool = False):
    """
    Remove finalizers from a Kubernetes object.
    - If is_new=True: modify local body (before creation)
    - If is_new=False: patch the existing object in cluster
    """
    if is_new:
        # Just return empty finalizers for local body before creation
        return {"metadata": {"finalizers": []}}

    # Determine group, version, plural
    if kind.lower() == "scaledobject":
        group, version, plural = "keda.sh", "v1alpha1", "scaledobjects"
    elif kind.lower() in ["vpa", "verticalpodautoscaler"]:
        group, version, plural = "autoscaling.k8s.io", "v1", "verticalpodautoscalers"
    else:
        logger.warning(f"⚠️ Unknown kind {kind}, cannot remove finalizers")
        return

    try:
        body = {"metadata": {"finalizers": []}}
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: custom_api.patch_namespaced_custom_object(
                group, version, namespace, plural, name, body
            )
        )
        logger.info(f"✅ Removed finalizers from existing {kind} {namespace}/{name}")
    except ApiException as e:
        if e.status == 404:
            logger.info(f"ℹ️ {kind} {namespace}/{name} not found, nothing to remove")
        else:
            logger.error(f"❌ Failed to remove finalizers from {kind} {namespace}/{name}: {e}")

async def create_scaledobject(namespace, name, kind, delay_seconds):
    """Create ScaledObject if not exists"""
    so_name = f"cpu-scaledobject-{name}"
    body = {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {"name": f"cpu-scaledobject-{name}",
                     "namespace": namespace,
                     "labels": {
                         "workload-kind": kind,
                         "workload-name": name
                        }
                    },
        "spec": {
            "scaleTargetRef": {"name": name},
            "minReplicaCount": DEFAULT_SCALEDOBJECT["minReplicaCount"],
            "maxReplicaCount": DEFAULT_SCALEDOBJECT["maxReplicaCount"],
            "triggers": [{
                "type": "cpu",
                "metricType": "Utilization",
                "metadata": {
                            "value": str(DEFAULT_SCALEDOBJECT["cpuTriggerValue"])}
            }],
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleUp": {
                            "stabilizationWindowSeconds": delay_seconds
                        }
                    }
                }
            }
        }
    }
    try:
        # Check existence to skip unnecessary sleep/delay
        existing = custom_api.get_namespaced_custom_object("keda.sh", "v1alpha1", namespace, "scaledobjects", so_name)
        
        # Check if the object is in deletion phase
        if existing.get("metadata", {}).get("deletionTimestamp"):
            logger.info(f"{so_name} is terminating. Waiting for full deletion before recreate...")
            raise ApiException(status=404)  # force recreation path
        
        # Check if delay is different → patch
        current_delay = (
            existing.get("spec", {})
            .get("advanced", {})
            .get("horizontalPodAutoscalerConfig", {})
            .get("behavior", {})
            .get("scaleUp", {})
            .get("stabilizationWindowSeconds")
        )
        if current_delay != delay_seconds:
            patch_body = {
                "spec": {
                    "advanced": {
                        "horizontalPodAutoscalerConfig": {
                            "behavior": {
                                "scaleUp": {
                                    "stabilizationWindowSeconds": delay_seconds
                                }
                            }
                        }
                    }
                }
            }
            custom_api.patch_namespaced_custom_object(
                "keda.sh", "v1alpha1", namespace, "scaledobjects", so_name, patch_body
            )
            logger.info(f"✏️ Patched ScaledObject {namespace}/{name} with new delay={delay_seconds}s")
        else:
            logger.info(f"ℹ️ ScaledObject already up-to-date for {namespace}/{name}")
        return
        
    except ApiException as e:
        if e.status != 404:
            raise
    
    body["metadata"].update(await remove_finalizers_safe(namespace, name, "ScaledObject", is_new=True))
    try:
        custom_api.create_namespaced_custom_object(
            group="keda.sh",
            version="v1alpha1",
            namespace=namespace,
            plural="scaledobjects",
            body=body,
        )
        logger.info(f"✅ Created ScaledObject for {namespace}/{name}")
    except ApiException as e:
        if e.status == 409:
            logger.info(f"ℹ️ ScaledObject already exists for {namespace}/{name}")
        else:
            logger.error(
                f"❌ Failed to create ScaledObject for {namespace}/{name} "
                f"(status={e.status}): {e.reason}"
            )


async def create_vpa(namespace, name, kind):
    """Create VPA if not exists"""
    vpa_name = f"vpa-{name}"
    
      # Check workload existence before creating
    try:
        if kind == "Deployment":
            apps_api.read_namespaced_deployment(name, namespace)
        elif kind == "StatefulSet":
            apps_api.read_namespaced_stateful_set(name, namespace)
        else:
            logger.warning(f"⚠️ Unknown workload kind {kind}, skipping VPA for {namespace}/{name}")
            return
    except ApiException as e:
        if e.status == 404:
            logger.info(f"⏭️ Skipping VPA creation: {kind} {namespace}/{name} not found.")
            return
        else:
            raise
    
    try:
        existing = custom_api.get_namespaced_custom_object("autoscaling.k8s.io", "v1", namespace, "verticalpodautoscalers", vpa_name)
        
        #  If it's in the process of being deleted, don’t treat it as "existing"
        if existing.get("metadata", {}).get("deletionTimestamp"):
            logger.info(f"⚠️ VPA {vpa_name} is terminating. Will recreate once fully deleted.")
            raise ApiException(status=404)
        
        logger.info(f"ℹ️ VPA already exists for {namespace}/{name}")
        return
    except ApiException as e:
        if e.status != 404:
            raise
    body = {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": f"vpa-{name}", 
                     "namespace": namespace,
                     "labels": {
                         "workload-kind": kind,
                         "workload-name": name
                        }
                    },
        "spec": {
            "targetRef": {"apiVersion": "apps/v1", "kind": kind, "name": name},
            "updatePolicy": {"updateMode": DEFAULT_VPA["updateMode"]},
            "resourcePolicy": {
                "containerPolicies": [{
                    "containerName": "*",
                    "minAllowed": DEFAULT_VPA["minAllowed"],
                    "maxAllowed": DEFAULT_VPA["maxAllowed"]
                }]
            }
        }
    }
    body["metadata"].update(await remove_finalizers_safe(namespace, name, "VPA", is_new=True))
    try:
        custom_api.create_namespaced_custom_object(
            group="autoscaling.k8s.io",
            version="v1",
            namespace=namespace,
            plural="verticalpodautoscalers",
            body=body,
        )
        logger.info(f"✅ Created VPA for {namespace}/{name}")
    except ApiException as e:
        if e.status == 409:
            logger.info(f"ℹ️ VPA already exists for {namespace}/{name}")
        else:
            logger.error(
                f"❌ Failed to create VPA for {namespace}/{name} "
                f"(status={e.status}): {e.reason}"
            )

##Helper funtion to convert time in HH:MM to total minutes
def hours_and_minutes_to_minutes(hours, minutes):
    duration = timedelta(hours=hours, minutes=minutes)
    return duration.total_seconds() / 60

## Scan at 18CET and delete the deployment and statefulset which is not running
async def daily_cleanup():
    """Delete unhealthy Deployments/StatefulSets at 18:00 CET"""   
    global last_cleanup_date
    
    tz = pytz.timezone("CET")
    now = datetime.now(tz)
    
     # Read cleanup time (float, e.g. 18.00 or 16.30)
    cleanup_time_str = os.getenv("DAILY_CLEANUP_HOUR", "14.55")  # Write the time in float using point and not colon
    try:
        cleanup_time = float(cleanup_time_str)
    except ValueError:
        logger.warning(f"⚠️ Invalid DAILY_CLEANUP_HOUR '{cleanup_time_str}', defaulting to 18.00")
        cleanup_time = 14.55

    # Convert cleanup time float (like 16.30) → total minutes
    cleanup_hours = int(cleanup_time)
    cleanup_minutes = int(round((cleanup_time - cleanup_hours) * 100))
    cleanup_total_minutes = hours_and_minutes_to_minutes(cleanup_hours, cleanup_minutes)

    # Convert current time → total minutes
    current_total_minutes = hours_and_minutes_to_minutes(now.hour, now.minute)
    
    logger.info(f"🕓 Checking if it's cleanup time... Current: {current_total_minutes}, Configured: {cleanup_total_minutes}")
    # Strict equality check (no tolerance)
    if int(current_total_minutes) != int(cleanup_total_minutes):
        return  # Not the configured cleanup time

    # Ensure it runs only once per day
    if last_cleanup_date and last_cleanup_date == (now.date(), cleanup_total_minutes):
        return  #Already ran today

    # Mark before running
    last_cleanup_date = (now.date(), cleanup_total_minutes)
    logger.info(f"🧹 Running daily cleanup scan at configured time ({cleanup_hours:02d}:{cleanup_minutes:02d} CET)")

    try:
        namespaces = [ns.metadata.name for ns in core_api.list_namespace().items]
    except ApiException as e:
        logger.error(f"❌ Failed to list namespaces during daily cleanup: {e}")
        return

    for ns in namespaces:
        if is_namespace_excluded(ns):
            continue

        try:
            # Check Deployments
            deployments = apps_api.list_namespaced_deployment(ns).items
            for dep in deployments:
                name = dep.metadata.name
                if not workload_fully_ready("Deployment", dep):
                    logger.info(f"🧹 Deployment {ns}/{name} not running — cleaning attached SO/VPA")
                    await cleanup(ns, name)

            # Check StatefulSets
            statefulsets = apps_api.list_namespaced_stateful_set(ns).items
            for sts in statefulsets:
                name = sts.metadata.name
                if not workload_fully_ready("StatefulSet", sts):
                    logger.info(f"🧹 StatefulSet {ns}/{name} not running — cleaning attached SO/VPA")
                    await cleanup(ns, name)

        except ApiException as error:
            logger.error(f"⚠️ Failed during cleanup in namespace {ns}: {error}")

    logger.info("✅ Daily cleanup complete for today.")

       
# -------------------------------------------------------------------
#  Cleanup function for ScaledObject and VPA
# -------------------------------------------------------------------
async def cleanup(namespace, name):
    """NEW: Reusable cleanup for both delete & annotation skip"""
    # Patch existing objects to remove finalizers first
    await remove_finalizers_safe(namespace, f"cpu-scaledobject-{name}", "ScaledObject", is_new=False)
    await remove_finalizers_safe(namespace, f"vpa-{name}", "VPA", is_new=False)
    
    # To patch finalizer into empty if anything was added by the controller in deployment and sttatefulset handlers
    try:
        apps_api.patch_namespaced_deployment(
            name, namespace, {"metadata": {"finalizers": []}}
        )
        logger.info(f"✅ Removed finalizers from Deployment {namespace}/{name}")
    except ApiException as e:
        if e.status != 404:
            logger.error(f"❌ Failed to remove Deployment finalizer: {e}")

    try:
        apps_api.patch_namespaced_stateful_set(
            name, namespace, {"metadata": {"finalizers": []}}
        )
        logger.info(f"✅ Removed finalizers from StatefulSet {namespace}/{name}")
    except ApiException as e:
        if e.status != 404:
            logger.error(f"❌ Failed to remove StatefulSet finalizer: {e}")
    
    for plural, group, version in [
        ("scaledobjects", "keda.sh", "v1alpha1"),
        ("verticalpodautoscalers", "autoscaling.k8s.io", "v1"),
    ]:
        try:
            custom_api.delete_namespaced_custom_object(
                group,
                version,
                namespace,
                plural,
                f"cpu-scaledobject-{name}" if plural == "scaledobjects" else f"vpa-{name}"
            )
            logger.info(f"🗑️ Deleted {plural} for {namespace}/{name}")
        except ApiException as e:
            if e.status == 404:
                logger.info(f"ℹ️ {plural} already deleted for {namespace}/{name}")
            else:
                raise

# -------------------------------------------------------------------
# Helper function for delayed execution
# -------------------------------------------------------------------
async def delayed_create(delay_seconds, func, *args, **kwargs):
    """Wait for delay_seconds and then call func"""
    if delay_seconds > 0:
        meta = kwargs.get("meta", {})
        resource_name = args[1] if len(args) > 1 else "unknown"
        logger.info(f"⏳ Waiting {delay_seconds//60} minutes before creating {func.__name__} for {resource_name}")
        await asyncio.sleep(delay_seconds)
    await func(*args, **kwargs)

# -------------------------------------------------------------------
# Self-healing: Recreate SO and VPA if missing
# -------------------------------------------------------------------
async def ensure_scaledobject_vpa(namespace, workload_name, workload_kind, spec, meta):
    """
    Checks if SO and VPA exist for a given workload; recreate if missing.
    Only recreates if the workload itself still exists.
    """
    logger.info(f"Kind: {workload_kind}, workload_name: {workload_name}, namespace: {namespace}")
    try:
        if workload_kind == "Deployment":
            apps_api.read_namespaced_deployment(workload_name, namespace)
        elif workload_kind == "StatefulSet":
            apps_api.read_namespaced_stateful_set(workload_name, namespace)
        else:
            logger.info(f"⚠️ Unknown workload kind {workload_kind} in {namespace}, skipping ensure check.")
            return
    except ApiException as e:
        if e.status == 404:
            logger.info(f"⏭️ Skipping recreate: {workload_kind} {namespace}/{workload_name} not found.")
            return
        else:
            raise
        
     # Use concurrency-safe wrapper so reconcile doesn’t overload
    await limited_handle_workload(spec, meta, namespace, workload_name, workload_kind)

# -------------------------------------------------------------------
#  Main handler
# -------------------------------------------------------------------
async def handle_workload(spec, meta, namespace, name, kind, **kwargs):
    try:
        ns_obj = core_api.read_namespace(namespace)
        if ns_obj.status.phase == "Terminating":
            logger.warning(f"⏭️ Skipping {namespace}/{name} because namespace is terminating")
            return
    except ApiException as e:
        if e.status == 404:
            logger.warning(f"⏭️ Skipping {namespace}/{name} because namespace not found")
            return
        else:
            raise
    
    annotations = meta.get("annotations", {})

    # Namespace exclusion
    if is_namespace_excluded(namespace):
        logger.info(f"⏭️ Skipping {namespace}/{name} due to namespace exclusion")
        return

    # Annotation skip with cleanup
    if annotations.get("tryg.workload-resource-controller", "true").lower() == "false":
        logger.info(f"⏭️ Skipping and cleaning {namespace}/{name} due to annotation")
        await cleanup(namespace, name)
        return
    
    # Delay (annotation override or default)
    delay_minutes = int(annotations.get("tryg.workload-resource-controller/delay", str(DEFAULT_DELAY_MINUTES)))
    if delay_minutes > MAX_DELAY_MINUTES:
        logger.warning(f"⚠️ Delay annotation too high ({delay_minutes} min). Bounding the value to {MAX_DELAY_MINUTES} min for {namespace}/{name}")
        delay_minutes = MAX_DELAY_MINUTES 
    delay_seconds = delay_minutes * 60  # Converted minutes to seconds
    
    #To ensure Deployment and statefulset is fully ready before creating SO and VPA
    try:
        if kind == "Deployment":
            obj = apps_api.read_namespaced_deployment(name, namespace)
        elif kind == "StatefulSet":
            obj = apps_api.read_namespaced_stateful_set(name, namespace)
        else:
            obj = None
        if obj and not workload_fully_ready(kind, obj):
            logger.info(f"⏭️ Skipping {namespace}/{name} because replicas not fully ready")
            return
    except ApiException as e:
        if e.status == 404:
            logger.warning(f"⏭️ Skipping {namespace}/{name} because workload not found")
            return
        else:
            raise
    
    # Create ScaledObject and VPA with helper for parallel execution and delay
    await asyncio.gather(
        delayed_create(delay_seconds, create_scaledobject, namespace, name, kind, delay_seconds),
        delayed_create(delay_seconds, create_vpa, namespace, name, kind)
    )
         
         
# -------------------------------------------------------------------
#  Handlers for Deployments & StatefulSets
# -------------------------------------------------------------------
@kopf.on.create("apps", "v1", "deployments")
@kopf.on.update("apps", "v1", "deployments")
async def deployment_handler(spec, meta, namespace, name, **kwargs):
    await limited_handle_workload(spec, meta, namespace, name, "Deployment", **kwargs)

@kopf.on.create("apps", "v1", "statefulsets")
@kopf.on.update("apps", "v1", "statefulsets")
async def sts_handler(spec, meta, namespace, name, **kwargs):
    await limited_handle_workload(spec, meta, namespace, name, "StatefulSet", **kwargs)


# -------------------------------------------------------------------
#  Cleanup handlers on deletion
# -------------------------------------------------------------------
@kopf.on.delete("apps", "v1", "deployments")
@kopf.on.delete("apps", "v1", "statefulsets")
async def delete_handler(meta, namespace, name, **kwargs):
    await cleanup(namespace, name)

# Controlling concurrency with semaphore
semaphore = asyncio.Semaphore(MAX_WORKERS)  # max tasks concurrently
async def limited_handle_workload(*args, **kwargs):
    async with semaphore:
        try:
            await handle_workload(*args, **kwargs)
        except Exception as e:
            logger.error(f"❌ Failed to handle workload: {e}")


# -------------------------------------------------------------------
#  Periodic reconciliation
# -------------------------------------------------------------------
async def periodic_reconcile(**kwargs):
    """
    Periodically scans all namespaces for Deployments and StatefulSets,
    ensuring ScaledObjects and VPAs are attached.
    """

    # List all namespaces
    try:
        namespaces = [ns.metadata.name for ns in core_api.list_namespace().items]
    except ApiException as e:
        logger.error(f"❌ Failed to list namespaces: {e}")
        return
    tasks = []
    
    for ns in namespaces:
        # Skip excluded namespaces
        if is_namespace_excluded(ns):
            continue
           
        # Handle Deployments
        try:
            deployments = apps_api.list_namespaced_deployment(ns).items
            for dep in deployments:
                dep_meta = api_client.sanitize_for_serialization(dep.metadata)
                annotations = dep.metadata.annotations or {}        
                # Skip workloads if annotation disables controller
                if annotations.get("tryg.workload-resource-controller", "true").lower() == "false":
                    logger.info(f"⏭️ Skipping {ns}/{dep.metadata.name} due to annotation")
                    continue
                # Skip if the workload is not fully ready
                if not workload_fully_ready("Deployment", dep):
                    logger.info(f"⏭️ Skipping {ns}/{dep.metadata.name} because replicas not fully ready")
                    continue
                tasks.append(limited_handle_workload(dep.spec, dep_meta, ns, dep.metadata.name, "Deployment"))
                tasks.append(ensure_scaledobject_vpa(ns, dep.metadata.name, "Deployment", dep.spec, dep_meta))
        except ApiException as e:
            logger.error(f"⚠️ Failed to list Deployments in namespace={ns}: {e}")

        # Handle StatefulSets
        try:
            statefulsets = apps_api.list_namespaced_stateful_set(ns).items
            for sts in statefulsets:
                sts_meta = api_client.sanitize_for_serialization(sts.metadata)
                annotations = sts.metadata.annotations or {}        
                # Skip workloads if annotation disables controller
                if annotations.get("tryg.workload-resource-controller", "true").lower() == "false":
                    logger.info(f"⏭️ Skipping {ns}/{sts.metadata.name} due to annotation")
                    continue
                # Skip if the workload is not fully ready    
                if not workload_fully_ready("StatefulSet", sts):
                    logger.info(f"⏭️ Skipping {ns}/{sts.metadata.name} because replicas not fully ready")
                    continue
                tasks.append(limited_handle_workload(sts.spec, sts_meta, ns, sts.metadata.name, "StatefulSet"))
                tasks.append(ensure_scaledobject_vpa(ns, sts.metadata.name, "StatefulSet", sts.spec, sts_meta))
        except ApiException as error:
            logger.error(f"⚠️ Failed to list Statefulsets in namespace={ns}: {error}")
        
        # Run all tasks concurrently
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("🔄 Periodic reconcile complete")
    
# -------------------------------------------------------------------
# Kopf startup: start FastAPI
# -------------------------------------------------------------------
@kopf.on.startup()
async def start_reconcile_loops(**kwargs):
    # ---- Run periodic reconcile loop ----
    async def reconcile_loop():
        while True:
            try:
                await periodic_reconcile()
            except Exception as error:
                logger.error(f"❌ Periodic reconcile failed: {error}")
            await asyncio.sleep(int(os.getenv("SLEEP_INTERVAL", "60")))

    # ---- Run daily cleanup loop ----
    async def cleanup_loop():
        while True:
            try:
                await daily_cleanup()
            except Exception as error:
                logger.error(f"❌ Daily cleanup failed: {error}")
            # Sleep for 60 seconds between checks so we don't miss the minute
            await asyncio.sleep(int(os.getenv("SLEEP_INTERVAL", "60")))
    
    # Start FastAPI in a separate thread
    def start_fastapi():
        uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")

    threading.Thread(target=start_fastapi, daemon=True).start()
    asyncio.create_task(reconcile_loop())
    asyncio.create_task(cleanup_loop())
    logger.info("🚀 Controller started with separate reconcile & cleanup loops")