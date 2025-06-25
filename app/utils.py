import boto3
import tempfile
from eks_token import get_token
from kubernetes import client, config
from kubernetes.client import ApiException
import base64
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
import os



MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
mongo_client = AsyncIOMotorClient(MONGO_URI)
log_db = mongo_client["fastapi_logs"]
log_collection = log_db["requests"]
REGION = "us-east-1"  # Change this to your AWS region



def configure_k8s_client(cluster_name):
    eks = boto3.client('eks', region_name=REGION)
    cluster_info = eks.describe_cluster(name=cluster_name)['cluster']
    endpoint = cluster_info["endpoint"]
    cluster_ca = cluster_info["certificateAuthority"]["data"]
    decoded_ca = base64.b64decode(cluster_ca)
    with tempfile.NamedTemporaryFile(delete=False, mode='wb') as ca_file:
        ca_file.write(decoded_ca)
        ca_file_path = ca_file.name
    token = get_token(cluster_name=cluster_name)['status']['token']
    kconfig = config.kube_config.Configuration(
        host=endpoint,
        api_key={'authorization': 'Bearer ' + token}
    )
    kconfig.ssl_ca_cert = ca_file_path
    kclient = client.ApiClient(configuration=kconfig)
    return client.CoreV1Api(api_client=kclient)



def configure_k8s_api_client(cluster_name):
    eks = boto3.client('eks', region_name=REGION)
    cluster_info = eks.describe_cluster(name=cluster_name)['cluster']
    endpoint = cluster_info["endpoint"]
    cluster_ca = cluster_info["certificateAuthority"]["data"]
    decoded_ca = base64.b64decode(cluster_ca)

    with tempfile.NamedTemporaryFile(delete=False, mode='wb') as ca_file:
        ca_file.write(decoded_ca)
        ca_file_path = ca_file.name

    token = get_token(cluster_name=cluster_name)['status']['token']
    kconfig = client.Configuration()
    kconfig.host = endpoint
    kconfig.ssl_ca_cert = ca_file_path
    kconfig.api_key = {"authorization": "Bearer " + token}
    kconfig.verify_ssl = True

    return client.ApiClient(configuration=kconfig)


class RequestLoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = datetime.utcnow()
        client_ip = request.client.host
        endpoint = request.url.path

        response = await call_next(request)

        # Log entry
        log_entry = {
            "timestamp": start_time,
            "ip": client_ip,
            "method": request.method,
            "endpoint": endpoint,
            "status_code": response.status_code
        }

        # Insert into MongoDB
        try:
            await log_collection.insert_one(log_entry)
        except Exception as e:
            print(f"Failed to log request: {e}")

        return response
