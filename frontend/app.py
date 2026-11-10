import streamlit as st
import httpx
import math

BACKEND_URL = "http://localhost:8000"
SESSION_ID = "default-engineer-session"

st.set_page_config(
    page_title="Agentic Ops | Production Troubleshooter",
    page_icon="🛠️",
    layout="wide",
)

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
        st.session_state["trigger_toast"] = ("Successfully connected to Jira!", "🎉")
    st.query_params.clear()

st.title("🛠️ Autonomous Production Incident Troubleshooter")
st.caption("Stage 1: Jira REST v3 Connector + Agentic StateGraph Diagnostic Orchestrator")


def fetch_connection_status():
    try:
        r = httpx.get(f"{BACKEND_URL}/auth/jira/status", params={"session_id": SESSION_ID}, timeout=5.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


# ---------------- CALLBACKS FOR SINGLE-RUN STATE UPDATES ----------------
def go_prev_page():
    if st.session_state.get("current_page", 1) > 1:
        st.session_state["current_page"] -= 1


def go_next_page():
    st.session_state["current_page"] = st.session_state.get("current_page", 1) + 1


def trigger_refresh():
    st.session_state.pop("workspace_projects", None)
    st.session_state.pop("tickets_cache", None)
    st.session_state["current_page"] = 1


def on_project_filter_change():
    st.session_state["current_page"] = 1


status_data = fetch_connection_status()

if "trigger_toast" in st.session_state:
    msg, icon = st.session_state["trigger_toast"]
    st.toast(msg, icon=icon)
    del st.session_state["trigger_toast"]

# ---------------- SIDEBAR: TOOL INTEGRATIONS ----------------
with st.sidebar:
    st.header("🔌 Tool Integrations")

    if status_data and status_data.get("connected"):
        st.success(f"🟢 Jira Connected ({status_data.get('auth_mode').upper()})")

        available_sites = status_data.get("available_sites", [])
        if available_sites and len(available_sites) > 1:
            site_options = {s["name"]: s["id"] for s in available_sites}
            current_site_name = status_data.get("site_name")

            selected_site_name = st.selectbox(
                "Switch Jira Workspace Site",
                options=list(site_options.keys()),
                index=list(site_options.keys()).index(current_site_name) if current_site_name in site_options else 0
            )

            selected_site_id = site_options[selected_site_name]
            if selected_site_id != status_data.get("cloud_id"):
                with st.spinner("Switching workspace site..."):
                    httpx.post(
                        f"{BACKEND_URL}/auth/jira/switch-site",
                        params={"site_id": selected_site_id, "session_id": SESSION_ID},
                        timeout=5.0,
                    )
                    st.session_state.pop("workspace_projects", None)
                    st.session_state.pop("tickets_cache", None)
                    st.session_state["current_page"] = 1
                    st.toast(f"Switched to workspace: {selected_site_name}", icon="🔄")
                    st.rerun()

        st.write(f"**Workspace:** {status_data.get('site_name')}")
        st.write(f"**Site URL:** `{status_data.get('site_url')}`")

        st.write("")
        if st.button("🔌 Disconnect Jira", type="secondary", use_container_width=True):
            with st.spinner("Disconnecting..."):
                httpx.post(f"{BACKEND_URL}/auth/jira/disconnect", params={"session_id": SESSION_ID}, timeout=5.0)
                st.session_state.pop("workspace_projects", None)
                st.session_state.pop("tickets_cache", None)
                st.session_state["current_page"] = 1
                st.toast("Successfully disconnected from Jira.", icon="🔌")
                st.rerun()
    else:
        st.warning("🔴 Jira Not Connected")

        auth_tab1, auth_tab2 = st.tabs(["🔐 OAuth 2.0 (3LO)", "🔑 API Token (Fast Test)"])

        with auth_tab1:
            st.markdown("Authenticate securely with Atlassian in this tab.")
            oauth_login_url = f"{BACKEND_URL}/auth/jira/login?session_id={SESSION_ID}"

            st.markdown(f"""
                <div style="text-align: center; margin-top: 10px;">
                    <a href="{oauth_login_url}" target="_self" style="display: block; width: 100%; background-color: #0052CC; color: white; padding: 10px 15px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 14px; box-sizing: border-box;">
                        🚀 Sign in with Atlassian (OAuth 2.0)
                    </a>
                </div>
            """, unsafe_allow_html=True)

        with auth_tab2:
            with st.form("pat_form"):
                site_url = st.text_input("Jira Workspace URL", placeholder="https://your-domain.atlassian.net")
                email = st.text_input("Atlassian Email", placeholder="you@company.com")
                api_token = st.text_input("API Token", type="password")
                submitted = st.form_submit_button("Connect Workspace", use_container_width=True)
                if submitted:
                    with st.spinner("Verifying credentials with Jira..."):
                        resp = httpx.post(
                            f"{BACKEND_URL}/auth/jira/connect-pat",
                            params={"session_id": SESSION_ID},
                            json={"site_url": site_url, "email": email, "api_token": api_token},
                            timeout=15.0,
                        )
                        if resp.status_code == 200:
                            st.session_state.pop("workspace_projects", None)
                            st.session_state.pop("tickets_cache", None)
                            st.session_state["current_page"] = 1
                            st.toast("Successfully connected to Jira via API Token!", icon="🎉")
                            st.rerun()
                        else:
                            st.error(f"Connection failed: {resp.text}")

    st.divider()
    st.subheader("☁️ Upcoming Stage 2 Connectors")
    st.checkbox("Azure Monitor / App Insights (Stage 2)", value=False, disabled=True)
    st.checkbox("AWS CloudWatch / Lambda Logs (Stage 2)", value=False, disabled=True)
    st.checkbox("Visual Studio Codebase Vector DB (Stage 3)", value=False, disabled=True)

# ---------------- MAIN WORKSPACE ----------------
if not status_data or not status_data.get("connected"):
    st.info("👈 Connect your Jira account in the left sidebar to browse live tickets and launch diagnostics.")
else:
    if "current_page" not in st.session_state:
        st.session_state["current_page"] = 1
    if "tickets_cache" not in st.session_state:
        st.session_state["tickets_cache"] = {}

    page_size = 10

    if "workspace_projects" not in st.session_state:
        try:
            p_resp = httpx.get(f"{BACKEND_URL}/jira/projects", params={"session_id": SESSION_ID}, timeout=10.0)
            if p_resp.status_code == 200:
                st.session_state["workspace_projects"] = p_resp.json()
            else:
                st.session_state["workspace_projects"] = []
        except Exception:
            st.session_state["workspace_projects"] = []

    with st.expander("📋 Browse Recent Tickets from Connected Jira Workspace", expanded=True):
        col_ctrl1, col_ctrl2 = st.columns([4, 1])

        projects = st.session_state.get("workspace_projects", [])

        proj_options = {"All Projects": "ALL"}
        for p in projects:
            proj_options[f"{p['name']} ({p['key']})"] = p["key"]

        with col_ctrl1:
            selected_proj_label = st.selectbox(
                "Filter by Project / Board",
                options=list(proj_options.keys()),
                key="project_filter_select",
                on_change=on_project_filter_change,
            )
            selected_proj_key = proj_options[selected_proj_label]

        with col_ctrl2:
            st.write("")
            st.write("")
            st.button("🔄 Refresh", on_click=trigger_refresh, use_container_width=True)

        current_page = st.session_state["current_page"]
        start_at = (current_page - 1) * page_size
        cache_key = f"{status_data.get('cloud_id')}_{selected_proj_key}_{current_page}"

        # Only call the backend once per unique (site, project, page) view
        if cache_key not in st.session_state["tickets_cache"]:
            with st.spinner("Fetching tickets from Jira workspace..."):
                try:
                    resp = httpx.get(
                        f"{BACKEND_URL}/jira/tickets",
                        params={
                            "start_at": start_at,
                            "max_results": page_size,
                            "project_key": selected_proj_key,
                            "session_id": SESSION_ID,
                        },
                        timeout=25.0,
                    )
                    if resp.status_code == 200:
                        st.session_state["tickets_cache"][cache_key] = resp.json()
                    else:
                        st.error(f"Error loading tickets: {resp.text}")
                        st.session_state["tickets_cache"][cache_key] = {"issues": [], "total": 0}
                except Exception as e:
                    st.error(f"Connection error: {e}")
                    st.session_state["tickets_cache"][cache_key] = {"issues": [], "total": 0}

        cached_data = st.session_state["tickets_cache"].get(cache_key, {"issues": [], "total": 0})
        filtered_tickets = cached_data.get("issues", [])
        total_available = cached_data.get("total", len(filtered_tickets))
        total_pages = max(1, math.ceil(total_available / page_size))

        if filtered_tickets:
            st.dataframe(filtered_tickets, use_container_width=True)

            first_item_num = start_at + 1
            last_item_num = start_at + len(filtered_tickets)

            if total_pages > 1:
                col_p1, col_p2, col_p3 = st.columns([1, 2, 1])
                with col_p1:
                    if current_page > 1:
                        st.button("⬅️ Previous Page", on_click=go_prev_page, use_container_width=True)
                with col_p2:
                    st.markdown(
                        f"<div style='text-align: center; padding-top: 6px;'>"
                        f"Page <b>{current_page}</b> of <b>{total_pages}</b> "
                        f"(Showing {first_item_num}–{last_item_num} of {total_available} tasks)"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with col_p3:
                    if current_page < total_pages:
                        st.button("Next Page ➡️", on_click=go_next_page, use_container_width=True)
            else:
                st.markdown(
                    f"<div style='text-align: center; color: gray; padding-top: 5px;'>"
                    f"Showing all {total_available} tasks for this view."
                    f"</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.warning("No tickets found matching the selected project filter.")

    st.divider()

    col_input, col_cloud, col_btn = st.columns([3, 2, 2])
    with col_input:
        default_key = filtered_tickets[0]["key"] if filtered_tickets else "KAN-1"
        ticket_key_input = st.text_input("Enter Jira Ticket ID to Diagnose", value=default_key)
    with col_cloud:
        cloud_hint = st.selectbox("Cloud Environment Override", ["AUTO", "AZURE", "AWS"])
    with col_btn:
        st.write("")
        st.write("")
        run_btn = st.button("🔍 Run Agentic Diagnostics", type="primary", use_container_width=True)

    if run_btn and ticket_key_input:
        with st.spinner(f"Executing Agentic StateGraph Loop for {ticket_key_input}..."):
            resp = httpx.post(
                f"{BACKEND_URL}/agent/investigate",
                json={
                    "session_id": SESSION_ID,
                    "ticket_key": ticket_key_input,
                    "cloud_provider_hint": cloud_hint,
                },
                timeout=60.0,
            )

        if resp.status_code != 200:
            st.error(f"Investigation Failed: {resp.text}")
        else:
            report = resp.json()
            t_data = report["ticket_data"]

            left_col, right_col = st.columns([1, 2])

            with left_col:
                st.subheader("⚡ Live Agent Execution Trace")
                for step in report["execution_trace"]:
                    with st.container(border=True):
                        st.markdown(f"**🟢 {step['step_name']}**")
                        st.caption(f"Tool: `{step['tool_used']}`")
                        st.write(step["details"])

                st.subheader("📌 Extracted Ticket Context")
                st.write(f"**Summary:** {t_data['summary']}")
                st.write(f"**Status:** `{t_data['status']}` | **Priority:** `{t_data['priority']}`")
                st.write(f"**Detected Cloud:** `{report['detected_cloud']}`")
                st.write(f"**Target Resource:** `{report['affected_resource_type']}`")
                with st.expander("Raw Ticket Description & Comments"):
                    st.write(t_data["description"])
                    if t_data["comments"]:
                        st.markdown("**Comments:**")
                        for c in t_data["comments"]:
                            st.markdown(f"- {c}")

            with right_col:
                st.subheader("🧠 Root Cause Analysis")
                st.info(report["root_cause_analysis"])

                st.subheader("🛠️ Recommended Engineering Fix")
                st.markdown(report["recommended_fix"])

                st.subheader("💬 Ready-to-Post Jira Update")
                st.code(report["draft_jira_comment"], language="markdown")