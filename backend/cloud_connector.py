import httpx
from importlib import import_module
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from fastapi import HTTPException
from datetime import datetime, timedelta


# ---------------- STANDALONE DATA MODELS ----------------
class CloudResourceProject(BaseModel):
    id: str
    name: str
    project_group: str
    provider: str  # "AZURE" or "AWS"


class CloudSessionState(BaseModel):
    session_id: str
    connected: bool = False
    provider: Optional[str] = None  # "AZURE" or "AWS"
    is_sandbox: bool = False
    account_label: Optional[str] = None
    region_or_subscription: Optional[str] = None
    azure_tenant_id: Optional[str] = None
    azure_client_id: Optional[str] = None
    azure_client_secret: Optional[str] = None
    azure_subscription_id: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region: str = "ap-south-1"
    available_resources: List[CloudResourceProject] = []
    selected_resource_id: str = "AUTO"


class AzureConnectRequest(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: str
    subscription_id: str
    use_sandbox: bool = False


class AWSConnectRequest(BaseModel):
    access_key_id: str
    secret_access_key: str
    region: str = "ap-south-1"
    use_sandbox: bool = False


_CLOUD_SESSIONS: Dict[str, CloudSessionState] = {}


def get_or_create_cloud_session(session_id: str) -> CloudSessionState:
    if session_id not in _CLOUD_SESSIONS:
        _CLOUD_SESSIONS[session_id] = CloudSessionState(session_id=session_id)
    return _CLOUD_SESSIONS[session_id]


def disconnect_cloud_session(session_id: str) -> CloudSessionState:
    _CLOUD_SESSIONS[session_id] = CloudSessionState(session_id=session_id)
    return _CLOUD_SESSIONS[session_id]


class CloudTelemetryConnector:
    """Independent, reusable connector for Azure Monitor & AWS CloudWatch."""

    @staticmethod
    async def connect_azure(session_id: str, req: AzureConnectRequest) -> CloudSessionState:
        session = get_or_create_cloud_session(session_id)

        if req.use_sandbox:
            session.connected = True
            session.provider = "AZURE"
            session.is_sandbox = True
            session.account_label = "Azure Enterprise Sandbox"
            session.region_or_subscription = req.subscription_id or "sub-prod-india-01"
            session.available_resources = [
                CloudResourceProject(
                    id="app-insights-order-fn",
                    name="order-processor-fn (App Insights)",
                    project_group="rg-ecommerce-prod",
                    provider="AZURE",
                ),
                CloudResourceProject(
                    id="app-insights-billing-api",
                    name="billing-service-api (App Insights)",
                    project_group="rg-billing-dev",
                    provider="AZURE",
                ),
            ]
            session.selected_resource_id = "AUTO"
            return session

        token_url = f"https://login.microsoftonline.com/{req.tenant_id}/oauth2/v2.0/token"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": req.client_id,
                    "client_secret": req.client_secret,
                    "scope": "https://management.azure.com/.default",
                },
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=401,
                    detail=f"Azure authentication failed: {resp.text}",
                )
            token_data = resp.json()
            access_token = token_data["access_token"]

            mgmt_url = (
                f"https://management.azure.com/subscriptions/{req.subscription_id}"
                f"/providers/Microsoft.Insights/components?api-version=2020-02-02"
            )
            res_resp = await client.get(
                mgmt_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if res_resp.status_code != 200:
                raise HTTPException(
                    status_code=res_resp.status_code,
                    detail=f"Failed to list Azure resources: {res_resp.text}",
                )

            items = res_resp.json().get("value", [])
            discovered: List[CloudResourceProject] = []
            for item in items:
                props = item.get("properties", {})
                app_id = props.get("AppId") or item.get("name", "unknown")
                name = item.get("name", "Unknown-App")
                raw_id = item.get("id", "")
                rg_part = "Default-RG"
                if "/resourceGroups/" in raw_id:
                    rg_part = raw_id.split("/resourceGroups/")[1].split("/")[0]
                discovered.append(
                    CloudResourceProject(
                        id=app_id,
                        name=f"{name} ({rg_part})",
                        project_group=rg_part,
                        provider="AZURE",
                    )
                )

        session.connected = True
        session.provider = "AZURE"
        session.is_sandbox = False
        session.account_label = f"Tenant: {req.tenant_id[:8]}..."
        session.region_or_subscription = req.subscription_id
        session.azure_tenant_id = req.tenant_id
        session.azure_client_id = req.client_id
        session.azure_client_secret = req.client_secret
        session.azure_subscription_id = req.subscription_id
        session.available_resources = discovered
        session.selected_resource_id = "AUTO"
        return session

    @staticmethod
    async def connect_aws(session_id: str, req: AWSConnectRequest) -> CloudSessionState:
        session = get_or_create_cloud_session(session_id)

        if req.use_sandbox:
            session.connected = True
            session.provider = "AWS"
            session.is_sandbox = True
            session.account_label = "AWS Sandbox Account"
            session.region_or_subscription = req.region
            session.available_resources = [
                CloudResourceProject(
                    id="/aws/lambda/order-processor-fn",
                    name="/aws/lambda/order-processor-fn",
                    project_group="Lambda-Prod",
                    provider="AWS",
                ),
                CloudResourceProject(
                    id="/aws/ecs/billing-service-dev",
                    name="/aws/ecs/billing-service-dev",
                    project_group="ECS-Billing",
                    provider="AWS",
                ),
            ]
            session.selected_resource_id = "AUTO"
            return session

        try:
            logs_client = import_module("boto3").client(
                "logs",
                aws_access_key_id=req.access_key_id,
                aws_secret_access_key=req.secret_access_key,
                region_name=req.region,
            )
            resp = logs_client.describe_log_groups(limit=50)
            log_groups = resp.get("logGroups", [])

            discovered = []
            for lg in log_groups:
                lg_name = lg.get("logGroupName", "")
                parts = [p for p in lg_name.split("/") if p]
                proj_group = parts[1] if len(parts) > 1 else "CloudWatch"
                discovered.append(
                    CloudResourceProject(
                        id=lg_name,
                        name=lg_name,
                        project_group=proj_group,
                        provider="AWS",
                    )
                )
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"AWS Connection Failed: {str(e)}")

        session.connected = True
        session.provider = "AWS"
        session.is_sandbox = False
        session.account_label = f"IAM Key: {req.access_key_id[:6]}***"
        session.region_or_subscription = req.region
        session.aws_access_key_id = req.access_key_id
        session.aws_secret_access_key = req.secret_access_key
        session.aws_region = req.region
        session.available_resources = discovered
        session.selected_resource_id = "AUTO"
        return session

    @staticmethod
    async def fetch_logs_for_resource(
        session: CloudSessionState,
        target_resource_hint: str = "",
        ticket_summary: str = "",
    ) -> Dict[str, str]:
        if not session.connected or not session.provider:
            return {
                "resolved_provider": "UNCONNECTED",
                "resolved_resource": "None",
                "logs": "No Cloud Provider connected. Connect Azure or AWS in the sidebar to pull live logs.",
            }

        chosen_resource: Optional[CloudResourceProject] = None
        if session.selected_resource_id != "AUTO":
            chosen_resource = next(
                (r for r in session.available_resources if r.id == session.selected_resource_id),
                None,
            )

        if not chosen_resource and session.available_resources:
            search_blob = f"{target_resource_hint} {ticket_summary}".lower()
            for r in session.available_resources:
                clean_name = r.name.split(" ")[0].lower()
                if clean_name in search_blob or r.project_group.lower() in search_blob:
                    chosen_resource = r
                    break
            if not chosen_resource:
                chosen_resource = session.available_resources[0]

        resource_id = chosen_resource.id if chosen_resource else target_resource_hint or "default-service"
        resource_label = chosen_resource.name if chosen_resource else resource_id

        if session.is_sandbox:
            now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            if session.provider == "AZURE":
                logs = (
                    f"[{now_str}] [Azure App Insights | {resource_label}]\n"
                    f"Exception: System.NullReferenceException: Object reference not set to an instance of an object.\n"
                    f"  at OrderProcessor.Function.ProcessOrderQueue(String payload, ILogger log) in /src/OrderProcessor/Function.cs:line 84\n"
                    f"Context: Incoming payload missing 'customer_wallet_id' field during transaction commit. HTTP 500."
                )
            else:
                logs = (
                    f"[{now_str}] [AWS CloudWatch | {resource_label}]\n"
                    f"ERROR [RequestId: 94f8a2b1-77c1] UnhandledPromiseRejection / TimeoutError: Connection pool exhausted\n"
                    f"  at Pool.acquire (/var/task/node_modules/pg-pool/index.js:145:11)\n"
                    f"REPORT RequestId: 94f8a2b1-77c1 Duration: 30002.45 ms Memory Size: 512 MB"
                )
            return {
                "resolved_provider": f"{session.provider} (Sandbox)",
                "resolved_resource": resource_label,
                "logs": logs,
            }

        if session.provider == "AZURE":
            token_url = f"https://login.microsoftonline.com/{session.azure_tenant_id}/oauth2/v2.0/token"
            async with httpx.AsyncClient(timeout=20.0) as client:
                t_resp = await client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": session.azure_client_id,
                        "client_secret": session.azure_client_secret,
                        "scope": "https://api.applicationinsights.io/.default",
                    },
                )
                if t_resp.status_code != 200:
                    return {
                        "resolved_provider": "AZURE",
                        "resolved_resource": resource_label,
                        "logs": f"Failed to acquire App Insights token: {t_resp.text}",
                    }
                ai_token = t_resp.json()["access_token"]
                kql = (
                    "exceptions | where timestamp > ago(7d) "
                    "| order by timestamp desc | take 10 "
                    "| project timestamp, type, outerMessage, details"
                )
                q_resp = await client.post(
                    f"https://api.applicationinsights.io/v1/apps/{resource_id}/query",
                    headers={"Authorization": f"Bearer {ai_token}"},
                    json={"query": kql},
                )
                if q_resp.status_code != 200:
                    return {
                        "resolved_provider": "AZURE",
                        "resolved_resource": resource_label,
                        "logs": f"App Insights Query Error: {q_resp.text}",
                    }
                rows = q_resp.json().get("tables", [{}])[0].get("rows", [])
                formatted = "\n".join([f"[{r[0]}] {r[1]}: {r[2]}" for r in rows]) or "No recent exceptions in last 7d."
                return {
                    "resolved_provider": "AZURE",
                    "resolved_resource": resource_label,
                    "logs": formatted,
                }

        if session.provider == "AWS":
            try:
                logs_client = import_module("boto3").client(
                    "logs",
                    aws_access_key_id=session.aws_access_key_id,
                    aws_secret_access_key=session.aws_secret_access_key,
                    region_name=session.aws_region,
                )
                events_resp = logs_client.filter_log_events(
                    logGroupName=resource_id,
                    limit=15,
                )
                events = events_resp.get("events", [])
                formatted = "\n".join(
                    [f"[{e.get('timestamp')}] {e.get('message', '').strip()}" for e in events]
                ) or "No recent log events found in this Log Group."
                return {
                    "resolved_provider": "AWS",
                    "resolved_resource": resource_label,
                    "logs": formatted,
                }
            except Exception as e:
                return {
                    "resolved_provider": "AWS",
                    "resolved_resource": resource_label,
                    "logs": f"CloudWatch filter_log_events error: {str(e)}",
                }

        return {
            "resolved_provider": session.provider,
            "resolved_resource": resource_label,
            "logs": "Unable to fetch logs.",
        }