# FDE Agentic Ops - Autonomous Production Incident Troubleshooter (Stage 1)

## Overview
An enterprise Agentic RAG system built with **FastAPI**, **LangGraph**, **Pydantic**, **Google Gemini (`gemini-3.6-flash`)**, and **Streamlit**.
Stage 1 connects to your Jira Cloud instance via **Atlassian OAuth 2.0 (3LO)** (or API Token fallback), dynamically resolves your company's `cloudId`, fetches live production tickets, classifies multi-cloud topology (Azure vs. AWS), runs a hybrid BM25 runbook lookup, and synthesizes a Root Cause Analysis and engineering fix.

## Quickstart Guide
1. Copy `.env.example` to `.env` and fill in your `GEMINI_API_KEY` (and optional Atlassian OAuth credentials).
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # macOS/Linux:
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Run the FastAPI Backend (Terminal 1):
   ```bash
   uvicorn backend.main:app --reload --port 8000
   ```
4. Run the Streamlit Dashboard (Terminal 2):
   ```bash
   streamlit run frontend/app.py --server.port 8501
   ```
