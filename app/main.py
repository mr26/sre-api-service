from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
import boto3
from kubernetes import client, config
from kubernetes.client import ApiException
from botocore.signers import RequestSigner
import base64
import tempfile
from eks_token import get_token
import time
import os
import redis.asyncio as redis
import json
from utils import configure_k8s_client, configure_k8s_api_client, RequestLoggerMiddleware
import logging



REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REGION = "us-east-1" 
DEFAULT_CLUSTER = "api-cluster-1"
TOKEN_TTL = 30


app = FastAPI()
app.add_middleware(RequestLoggerMiddleware)
redis_client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
logging.basicConfig(
    level=logging.INFO,  # Change to DEBUG for more detail
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)



def make_cache_key(endpoint: str, cluster_name: str, namespace: str = None):
    return f"{endpoint}:{cluster_name}:{namespace or 'all'}"



def list_eks_clusters():
    eks_client = boto3.client('eks', region_name=REGION)
    response = eks_client.list_clusters()
    return response.get('clusters', [])



@app.get("/clusters/{cluster_name}/deployments")
async def get_deployments(cluster_name: str, namespace: str = Query(None, description="Optional namespace")):
    logger.info(f"Received request for deployments in cluster (namespace: {namespace}): {cluster_name}")
    key = make_cache_key("deployments", cluster_name, namespace)
    cached = await redis_client.get(key)
    if cached:
        logger.info("Cache hit")
        return {
            "source": "redis",
            "data": json.loads(cached)
        }
    try:
        logger.info("Cache miss, calling Kubernetes API")
        k8s_client = await run_in_threadpool(configure_k8s_api_client, cluster_name)
        apps_v1 = client.AppsV1Api(k8s_client)
        if namespace:
            deployments = await run_in_threadpool(apps_v1.list_namespaced_deployment, namespace)
        else:
            deployments = await run_in_threadpool(apps_v1.list_deployment_for_all_namespaces)

        return_data =  {
            "cluster": cluster_name,
            "namespace": namespace or "all",
            "deployments": [
                {
                    "name": d.metadata.name,
                    "namespace": d.metadata.namespace,
                    "replicas": d.spec.replicas,
                    "available_replicas": d.status.available_replicas,
                    "labels": d.metadata.labels,
                }
                for d in deployments.items
            ]
        }
        await redis_client.set(key, json.dumps(return_data), ex=60)
        logger.info("Data cached successfully")
        return {
            "source": "kubernetes",
            "data": return_data
        }
    except ApiException as e:
        logger.error(f"Kubernetes API exception: {e}")
        return JSONResponse(status_code=e.status, content={"error": e.reason})
    except Exception as e:
        logger.exception(f"Unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    


@app.get("/clusters/{cluster_name}/services")
async def get_services(cluster_name: str, namespace: str = Query(None, description="Optional namespace")):
    logger.info(f"Received request for services in cluster (namespace: {namespace}): {cluster_name}")
    key = make_cache_key("services", cluster_name, namespace)
    cached = await redis_client.get(key)
    if cached:
        logger.info("Cache hit")
        return {
            "source": "redis",
            "data": json.loads(cached)
        }
    try:
        logger.info("Cache miss, calling Kubernetes API")
        k8s_client = await run_in_threadpool(configure_k8s_client, cluster_name)
        if namespace:
            services = await run_in_threadpool(k8s_client.list_namespaced_service, namespace)
        else:
            services = await run_in_threadpool(k8s_client.list_service_for_all_namespaces)
        return_data =  {
            "cluster": cluster_name,
            "namespace": namespace or "all",
            "services": [
                {
                    "name": svc.metadata.name,
                    "namespace": svc.metadata.namespace,
                    "type": svc.spec.type,
                    "cluster_ip": svc.spec.cluster_ip,
                    "ports": [
                        {
                            "port": port.port,
                            "target_port": port.target_port,
                            "protocol": port.protocol
                        }
                        for port in svc.spec.ports
                    ],
                    "labels": svc.metadata.labels,
                }
                for svc in services.items
            ]
        }
        await redis_client.set(key, json.dumps(return_data), ex=60)
        logger.info("Data cached successfully")
        return {
            "source": "kubernetes",
            "data": return_data
        }
    except ApiException as e:
        logger.error(f"Kubernetes API exception: {e}")
        return JSONResponse(status_code=e.status, content={"error": e.reason})
    except Exception as e:
        logger.exception(f"Unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    


@app.get("/clusters/{cluster_name}/events")
async def get_events(cluster_name: str, namespace: str = Query(None, description="Namespace to filter events")):
    logger.info(f"Received request for events in cluster (namespace: {namespace}): {cluster_name}")
    key = make_cache_key("events", cluster_name, namespace)
    cached = await redis_client.get(key)
    if cached:
        logger.info("Cache hit")
        return {
            "source": "redis",
            "data": json.loads(cached)
        }
    try:
        logger.info("Cache miss, calling Kubernetes API")
        k8s_client = await run_in_threadpool(configure_k8s_client, cluster_name)
        if namespace:
            events = await run_in_threadpool(k8s_client.list_namespaced_event, namespace)
        else:
            events = await run_in_threadpool(k8s_client.list_event_for_all_namespaces)
        
        return_data =  {
            "cluster": cluster_name,
            "namespace": namespace if namespace else "all",
            "events": [
                {
                    "name": event.metadata.name,
                    "namespace": event.metadata.namespace,
                    "reason": event.reason,
                    "message": event.message,
                    "type": event.type,
                    "source": event.source.component if event.source else None,
                    "first_timestamp": event.first_timestamp.isoformat() if event.first_timestamp else None,
                    "last_timestamp": event.last_timestamp.isoformat() if event.last_timestamp else None,
                    "count": event.count,
                }
                for event in events.items
            ]
        }
        await redis_client.set(key, json.dumps(return_data), ex=60)
        logger.info("Data cached successfully")
        return {
            "source": "kubernetes",
            "data": return_data
        }
    except ApiException as e:
        logger.error(f"Kubernetes API exception: {e}")
        return JSONResponse(status_code=e.status, content={"error": e.reason})
    except Exception as e:
        logger.exception(f"Unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/clusters/{cluster_name}/pods")
async def get_pods(cluster_name: str, namespace: str = Query(None, description="Namespace to filter pods")):
    logger.info(f"Received request for pods in cluster (namespace: {namespace}): {cluster_name}")
    key = make_cache_key("pods", cluster_name, namespace)
    cached = await redis_client.get(key)
    if cached:
        logger.info("Cache hit")
        return {
            "source": "redis",
            "data": json.loads(cached)
        }
    try:
        logger.info("Cache miss, calling Kubernetes API")
        k8s_client = await run_in_threadpool(configure_k8s_client, cluster_name)
        if namespace:
            pods = await run_in_threadpool(k8s_client.list_namespaced_pod, namespace)
        else:
            pods = await run_in_threadpool(k8s_client.list_pod_for_all_namespaces)
        return_data =  {
            "cluster": cluster_name,
            "namespace": namespace if namespace else "all",
            "pods": [
                {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "node_name": pod.spec.node_name,
                    "status": pod.status.phase,
                    "labels": pod.metadata.labels,
                }
                for pod in pods.items
            ]
        }
        await redis_client.set(key, json.dumps(return_data), ex=60)
        logger.info("Data cached successfully")
        return {
            "source": "kubernetes",
            "data": return_data
        }
    except ApiException as e:
        logger.error(f"Kubernetes API exception: {e}")
        return JSONResponse(status_code=e.status, content={"error": e.reason})
    except Exception as e:
        logger.exception(f"Unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/clusters/{cluster_name}/namespaces")
async def get_namespaces(cluster_name: str):
    logger.info(f"Received request for namespaces in cluster: {cluster_name}")
    key = make_cache_key("namespaces", cluster_name)
    cached = await redis_client.get(key)
    if cached:
        logger.info("Cache hit")
        return {
            "source": "redis",
            "data": json.loads(cached)
        }
    try:
        logger.info("Cache miss, calling Kubernetes API")
        k8s_client = await run_in_threadpool(configure_k8s_client, cluster_name)
        namespaces = await run_in_threadpool(k8s_client.list_namespace)
        return_data =  {
            "cluster": cluster_name,
            "namespaces": [ns.metadata.name for ns in namespaces.items]
        }
        await redis_client.set(key, json.dumps(return_data), ex=60)
        logger.info("Data cached successfully")
        return {
            "source": "kubernetes",
            "data": return_data
        }

    except ApiException as e:
        logger.error(f"Kubernetes API exception: {e}")
        return JSONResponse(status_code=e.status, content={"error": e.reason})
    except Exception as e:
        logger.exception(f"Unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    


@app.get("/clusters")
async def get_clusters():
    logger.info(f"Received request to get all clusters.")
    key = "eks:clusters"
    cached = await redis_client.get(key)
    if cached:
        logger.info("Cache hit")
        return {
            "source": "redis",
            "clusters": json.loads(cached)
        }

    try:
        logger.info("Cache miss, calling AWS API")
        clusters = await run_in_threadpool(list_eks_clusters)
        await redis_client.set(key, json.dumps(clusters), ex=60)
        logger.info("Data cached successfully")
        return {"source": "aws", "clusters": clusters}
    except Exception as e:
        logger.exception(f"Unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/clusters/{cluster_name}/nodes")
async def get_nodes(cluster_name: str):
    logger.info(f"Received request for nodes in cluster: {cluster_name}")
    key = make_cache_key("nodes", cluster_name)
    cached = await redis_client.get(key)
    if cached:
        logger.info("Cache hit")
        return {
            "source": "redis",
            "data": json.loads(cached)
        }
    try:
        logger.info("Cache miss, calling Kubernetes API")
        k8s_client = await run_in_threadpool(configure_k8s_client, cluster_name)
        nodes = await run_in_threadpool(k8s_client.list_node)
        return_data =  {
            "nodes": [
                {
                    "name": node.metadata.name,
                    "labels": node.metadata.labels
                }
                for node in nodes.items
            ]
        }
        await redis_client.set(key, json.dumps(return_data), ex=60)
        logger.info("Data cached successfully")
        return {
            "source": "kubernetes",
            "data": return_data
        }
    except ApiException as e:
        logger.error(f"Kubernetes API exception: {e}")
        return JSONResponse(status_code=500, content={"error": e.reason})
    except Exception as e:
        logger.exception(f"Unexpected error occurred: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})