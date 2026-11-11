import streamlit as st
import httpx

BACKEND_URL = "http://localhost:8000"
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
    st.query_params.clear()


def fetch_connection_status():
    try:
        r = httpx.get(f"{BACKEND_URL}/auth/jira/status", params={"session_id": SESSION_ID}, timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def fetch_cloud_status():
    try:
        r = httpx.get(f"{BACKEND_URL}/cloud/status", params={"session_id": SESSION_ID}, timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


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
            "content": "👋 **Hello Engineer!** I am your **Autonomous AI Reliability Engineer**. Connect your workspace in the sidebar, then type a request (e.g., *'Explain KAN-1'*, *'Give me full details of KAN-1'*, *'How many tasks are there total'*, or *'Investigate KAN-1'*) to query your board.",
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
                            st.rerun()
                        else:
                            st.error(f"Error: {resp.text}")

    st.divider()
    st.markdown("#### ⚙️ Settings")
    st.checkbox("Human-in-the-Loop Review Gate", value=True)

# ---------------- MAIN WORKSPACE: GEMINI-STYLE CONVERSATIONAL CHAT ----------------
st.markdown("## 🤖 Autonomous Production Troubleshooter")
st.caption("Chat naturally with your AI Reliability Engineer to query workspace items, summarize tasks, or run diagnostics.")
st.divider()

for idx, message in enumerate(st.session_state["messages"]):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if "report" in message:
            rep = message["report"]
            tab_trace, tab_rca, tab_fix, tab_comment = st.tabs(["⚡ Execution Trace", "🧠 Analysis", "🛠️ Fix", "💬 Comment"])
            
            with tab_trace:
                for step in rep["execution_trace"]:
                    with st.container(border=True):
                        st.markdown(f"**🟢 {step['step_name']}**")
                        st.caption(f"Tool: `{step['tool_used']}`")
                        st.code(step["details"], language="text")
            with tab_rca:
                st.info(rep["root_cause_analysis"])
            with tab_fix:
                st.markdown(rep["recommended_fix"])
            with tab_comment:
                st.code(rep["draft_jira_comment"], language="markdown")
                ticket_key_val = rep['ticket_data'].get('ticket_key', 'GENERAL-1')
                
                # FIX: Using globally unique key combining ticket_key and message loop index (idx)
                if st.button("🚀 Approve & Post Update to Jira", key=f"btn_{ticket_key_val}_{idx}"):
                    st.success("Successfully posted diagnostic report to Jira ticket comments!")

prompt = st.chat_input("Ask anything (e.g., 'Explain KAN-1', 'Give me full details of KAN-1', or 'Investigate KAN-1')...")

if prompt:
    st.session_state["messages"].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner(f"🤖 Processing query: '{prompt}'..."):
            try:
                resp = httpx.post(
                    f"{BACKEND_URL}/agent/investigate",
                    json={
                        "session_id": SESSION_ID,
                        "ticket_key": prompt,
                        "cloud_provider_hint": "AUTO",
                    },
                    timeout=60.0,
                )

                if resp.status_code == 200:
                    report = resp.json()
                    response_text = report["root_cause_analysis"]
                    
                    st.markdown(response_text)
                    
                    if report["execution_trace"]:
                        tab_trace, tab_rca, tab_fix, tab_comment = st.tabs(["⚡ Execution Trace", "🧠 Analysis", "🛠️ Fix", "💬 Comment"])
                        with tab_trace:
                            for step in report["execution_trace"]:
                                with st.container(border=True):
                                    st.markdown(f"**🟢 {step['step_name']}**")
                                    st.caption(f"Tool: `{step['tool_used']}`")
                                    st.code(step["details"], language="text")
                        with tab_rca:
                            st.info(report["root_cause_analysis"])
                        with tab_fix:
                            st.markdown(report["recommended_fix"])
                        with tab_comment:
                            st.code(report["draft_jira_comment"], language="markdown")

                    st.session_state["messages"].append({
                        "role": "assistant",
                        "content": response_text,
                        "report": report
                    })
                else:
                    err_msg = f"Query failed: {resp.text}"
                    st.error(err_msg)
                    st.session_state["messages"].append({"role": "assistant", "content": err_msg})
            except Exception as e:
                err_msg = f"Connection error: {e}"
                st.error(err_msg)
                st.session_state["messages"].append({"role": "assistant", "content": err_msg})