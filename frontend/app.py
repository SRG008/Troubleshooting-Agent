import streamlit as st
import httpx
import json
import time

BACKEND_URL = "http://127.0.0.1:8000"
SESSION_ID = "default-engineer-session"

st.set_page_config(
    page_title="Agentic Ops | Autonomous Production Troubleshooter",
    page_icon="🤖",
    layout="wide",
)

st.markdown("""
    <style>
        h1, h2, h3, h4 {
            font-family: 'Inter', sans-serif;
        }
        .stButton>button {
            border-radius: 6px;
            font-weight: 600;
        }
    </style>
""", unsafe_allow_html=True)

st.markdown("""
    <script>
        if (window.location.search.includes('action=')) {
            const cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
            window.history.replaceState({path: cleanUrl}, '', cleanUrl);
        }
    </script>
""", unsafe_allow_html=True)

query_params = st.query_params
if "action" in query_params:
    action_val = query_params["action"]
    if action_val == "connected":
        st.session_state["trigger_toast"] = ("Successfully connected to Jira workspace via OAuth!", "🎉")
        st.session_state["refresh_status"] = True
    st.query_params.clear()


@st.cache_data(ttl=60, show_spinner=False)
def _get_status(path):
    # Raises on failure so that failed responses are not cached.
    r = httpx.get(f"{BACKEND_URL}{path}", params={"session_id": SESSION_ID}, timeout=5.0)
    r.raise_for_status()
    return r.json()


def fetch_connection_status():
    try:
        return _get_status("/auth/jira/status")
    except Exception:
        return None


def fetch_cloud_status():
    try:
        return _get_status("/cloud/status")
    except Exception:
        return None


def refresh_status_cache():
    _get_status.clear()


if st.session_state.pop("refresh_status", False):
    refresh_status_cache()
status_data = fetch_connection_status()
cloud_status = fetch_cloud_status()

if "trigger_toast" in st.session_state:
    msg, icon = st.session_state["trigger_toast"]
    st.toast(msg, icon=icon)
    del st.session_state["trigger_toast"]

if "messages" not in st.session_state:
    st.session_state["messages"] = [
        {
            "role": "assistant",
            "content": "👋 **Hello!** I am an AI assistant using Copilot SDK in VS Code. Connect your workspace in the sidebar, then ask anything about your projects, boards, sprints, or work items, for example *'How many open bugs are in project X?'*, *'Show the active sprint for board Y'*, or *'What is KAN-1 about?'*.",
        }
    ]

# ---------------- SIDEBAR: PROFESSIONAL NAVIGATION & CREDENTIALS ----------------
with st.sidebar:
    st.markdown("### ⚡ Agentic Ops Hub")
    st.caption("Enterprise Autonomous Reliability Platform")
    st.divider()

    # --- JIRA CONNECTION ---
    st.markdown("#### 📋 Issue Tracker Integration")
    if status_data and status_data.get("connected"):
        st.success("🟢 Connected")
        st.caption(f"Workspace: `{status_data.get('site_name')}`")
        if st.button("Disconnect Workspace", type="secondary", use_container_width=True):
            httpx.post(f"{BACKEND_URL}/auth/jira/disconnect", params={"session_id": SESSION_ID}, timeout=5.0)
            st.toast("Disconnected.", icon="🔌")
            refresh_status_cache()
            st.rerun()
    else:
        st.warning("🔴 Disconnected")
        jira_tab1, jira_tab2 = st.tabs(["🔐 OAuth 2.0", "🔑 API Token"])
        
        with jira_tab1:
            oauth_login_url = f"{BACKEND_URL}/auth/jira/login?session_id={SESSION_ID}"
            st.markdown(f"""
                <div style="text-align: center; margin-top: 8px; margin-bottom: 8px;">
                    <a href="{oauth_login_url}" target="_self" style="display: block; width: 100%; background-color: #0052CC; color: white; padding: 9px 12px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 13px; box-sizing: border-box;">
                        🚀 Sign in with Atlassian
                    </a>
                </div>
            """, unsafe_allow_html=True)
            
        with jira_tab2:
            with st.form("pat_auth_form"):
                site_url = st.text_input("Workspace URL", placeholder="https://domain.atlassian.net")
                email = st.text_input("Email", placeholder="you@company.com")
                api_token = st.text_input("API Token", type="password")
                submitted_pat = st.form_submit_button("Connect via Token", use_container_width=True)
                if submitted_pat:
                    with st.spinner("Connecting..."):
                        resp = httpx.post(
                            f"{BACKEND_URL}/auth/jira/connect-pat",
                            params={"session_id": SESSION_ID},
                            json={"site_url": site_url, "email": email, "api_token": api_token},
                            timeout=15.0,
                        )
                        if resp.status_code == 200:
                            st.toast("Connected successfully!", icon="🎉")
                            refresh_status_cache()
                            st.rerun()
                        else:
                            st.error(f"Failed: {resp.text}")

    st.divider()

    # --- CLOUD TELEMETRY CONNECTION ---
    st.markdown("#### ☁️ Cloud Telemetry")
    if cloud_status and cloud_status.get("connected"):
        mode_label = "SANDBOX" if cloud_status.get("is_sandbox") else "LIVE"
        st.success(f"🟢 {cloud_status.get('provider')} Connected ({mode_label})")
        if st.button("Disconnect Cloud", type="secondary", use_container_width=True):
            httpx.post(f"{BACKEND_URL}/cloud/disconnect", params={"session_id": SESSION_ID}, timeout=5.0)
            st.toast("Disconnected Cloud Provider.", icon="🔌")
            refresh_status_cache()
            st.rerun()
    else:
        st.warning("🔴 Cloud Unconnected")
        chosen_provider = st.radio("Platform", ["Azure Monitor", "AWS CloudWatch"], horizontal=True)
        use_sandbox = st.checkbox("🧪 Use Sandbox Mode", value=False)

        if use_sandbox:
            if st.button("🚀 Connect Sandbox Environment", use_container_width=True, type="primary"):
                with st.spinner("Initializing sandbox telemetry..."):
                    endpoint = "azure" if "Azure" in chosen_provider else "aws"
                    payload = {
                        "tenant_id": "sandbox-tenant", "client_id": "sandbox-client",
                        "client_secret": "sandbox-secret", "subscription_id": "sub-prod-india-01", "use_sandbox": True
                    } if endpoint == "azure" else {
                        "access_key_id": "sandbox-key", "secret_access_key": "sandbox-secret",
                        "region": "ap-south-1", "use_sandbox": True
                    }
                    resp = httpx.post(f"{BACKEND_URL}/cloud/connect/{endpoint}", params={"session_id": SESSION_ID}, json=payload, timeout=10.0)
                    if resp.status_code == 200:
                        st.toast("Sandbox connected successfully!", icon="☁️")
                        refresh_status_cache()
                        st.rerun()
                    else:
                        st.error(f"Error: {resp.text}")
        else:
            if "Azure" in chosen_provider:
                with st.form("azure_creds_form"):
                    tenant_id = st.text_input("Tenant ID", placeholder="tenant-id")
                    client_id = st.text_input("Client ID", placeholder="client-id")
                    client_secret = st.text_input("Client Secret", type="password", placeholder="client-secret")
                    subscription_id = st.text_input("Subscription ID", placeholder="subscription-id")
                    if st.form_submit_button("🚀 Connect Azure", use_container_width=True):
                        resp = httpx.post(f"{BACKEND_URL}/cloud/connect/azure", params={"session_id": SESSION_ID}, json={"tenant_id": tenant_id, "client_id": client_id, "client_secret": client_secret, "subscription_id": subscription_id, "use_sandbox": False}, timeout=20.0)
                        if resp.status_code == 200:
                            st.toast("Azure connected!", icon="☁️")
                            refresh_status_cache()
                            st.rerun()
                        else:
                            st.error(f"Error: {resp.text}")
            else:
                with st.form("aws_creds_form"):
                    access_key = st.text_input("AWS Access Key ID", placeholder="AKIA...")
                    secret_key = st.text_input("AWS Secret Access Key", type="password", placeholder="secret...")
                    aws_region = st.text_input("AWS Region", value="ap-south-1")
                    if st.form_submit_button("🚀 Connect AWS", use_container_width=True):
                        resp = httpx.post(f"{BACKEND_URL}/cloud/connect/aws", params={"session_id": SESSION_ID}, json={"access_key_id": access_key, "secret_access_key": secret_key, "region": aws_region, "use_sandbox": False}, timeout=20.0)
                        if resp.status_code == 200:
                            st.toast("AWS connected!", icon="☁️")
                            refresh_status_cache()
                            st.rerun()
                        else:
                            st.error(f"Error: {resp.text}")

    st.divider()
    st.markdown("#### ⚙️ Settings")
    st.checkbox("Human-in-the-Loop Review Gate", value=True)

# ---------------- MAIN WORKSPACE: GEMINI-STYLE CONVERSATIONAL CHAT ----------------
st.markdown("## 🤖 Autonomous Production Troubleshooter")
st.caption("Ask any question about your connected workspace: boards, sprints, work items, counts, or details.")
st.divider()

def render_message(message):
    # Same element tree as a live run (status, then answer). Streamlit greys out
    # elements from the previous run that were not re-emitted, so the tree must match.
    if message["role"] == "assistant" and "status" in message:
        label, state = message["status"]
        st.status(label, state=state, expanded=False)
    st.markdown(message["content"])


# The input is at page level so Streamlit pins it to the bottom of the screen.
prompt = st.chat_input("Ask anything about your boards, sprints, or work items...")

if prompt:
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state["messages"]
        if not m.get("pending")
    ]
    st.session_state["messages"].append({"role": "user", "content": prompt})
    st.session_state["messages"].append(
        {"role": "assistant", "content": "", "pending": True, "question": prompt, "history": history}
    )


def render_conversation():
    for message in st.session_state["messages"]:
        with st.chat_message(message["role"]):
            if not message.get("pending"):
                render_message(message)
                continue

            started = time.monotonic()
            status = st.status("Thinking...", expanded=False)
            answer_box = st.empty()
            text = ""
            finished = False
            last_draw = 0.0
            try:
                with httpx.stream(
                    "POST",
                    f"{BACKEND_URL}/agent/chat/stream",
                    json={
                        "session_id": SESSION_ID,
                        "message": message["question"],
                        "history": message["history"],
                    },
                    timeout=120.0,
                ) as resp:
                    if resp.status_code != 200:
                        resp.read()
                        message["content"] = f"Query failed: {resp.text}"
                        message["trace"] = []
                        message["status"] = ("Query failed", "error")
                        status.update(label="Query failed", state="error")
                        finished = True
                    else:
                        for line in resp.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            event = json.loads(line[5:])
                            kind = event.get("type")
                            if kind == "delta":
                                text += event["text"]
                                if time.monotonic() - last_draw > 0.05:
                                    answer_box.markdown(text + " ▌")
                                    last_draw = time.monotonic()
                            elif kind == "reset":
                                text = ""
                                answer_box.empty()
                            elif kind == "status":
                                status.update(label=event.get("message", "Working..."))
                            elif kind == "done":
                                message["content"] = event.get("answer", "")
                                message["trace"] = event.get("execution_trace", [])
                                message["elapsed"] = time.monotonic() - started
                                label = f"Answered in {message['elapsed']:.1f}s"
                                message["status"] = (label, "complete")
                                status.update(label=label, state="complete")
                                finished = True
                        if not finished:
                            message["content"] = text or "The connection ended before an answer was received."
                            message["trace"] = []
                            message["status"] = ("Connection ended early", "error")
                            status.update(label="Connection ended early", state="error")
            except Exception as e:
                message["content"] = f"Connection error: {e}"
                message["trace"] = []
                message["status"] = ("Connection error", "error")
                status.update(label="Connection error", state="error")

            # Keep the streamed answer in place so the element tree matches the reloaded one.
            answer_box.markdown(message["content"])
            message.pop("pending", None)
            message.pop("question", None)
            message.pop("history", None)


chat_slot = st.empty()
with chat_slot.container():
    render_conversation()

