"""Streamlit front end. Talks to the API only; it never touches the database."""
import os

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

API = os.environ.get("ASKBASE_API_URL", "http://localhost:8000").rstrip("/")

st.set_page_config(page_title="Askbase", page_icon="🔎", layout="wide")


def api(method: str, path: str, **kw):
    try:
        r = httpx.request(method, f"{API}{path}", timeout=90, **kw)
        if r.status_code == 429:
            return {"status": "rate_limited", "reason": "Too many requests. Wait a minute and try again."}
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return {"status": "error", "reason": f"The API is unavailable ({exc.__class__.__name__})."}


@st.cache_data(ttl=30)
def status():
    return api("GET", "/api/status")


def render_chart(chart: dict, columns: list[str], rows: list[list]):
    df = pd.DataFrame(rows, columns=columns)
    if chart["type"] == "line":
        fig = px.line(df, x=chart["x"], y=chart["y"], markers=True, title=chart["title"])
    elif chart["type"] == "pie":
        fig = px.pie(df, names=chart["x"], values=chart["y"][0], title=chart["title"])
    elif chart.get("horizontal"):
        fig = px.bar(df, y=chart["x"], x=chart["y"][0], orientation="h", title=chart["title"])
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
    else:
        fig = px.bar(df, x=chart["x"], y=chart["y"][0], title=chart["title"])
    fig.update_layout(margin={"l": 10, "r": 10, "t": 40, "b": 10}, height=360)
    st.plotly_chart(fig, use_container_width=True)


def render_answer(a: dict):
    status_ = a.get("status")
    if status_ == "blocked":
        st.error(f"🛡️ **Blocked by the guardrail, nothing was executed.** {a.get('reason', '')}")
    elif status_ == "refused":
        st.warning(a.get("reason") or "I can't help with that.")
    elif status_ in ("error", "no_llm", "rate_limited", "not_recorded"):
        st.info(a.get("reason") or "Something went wrong.")
    if a.get("answer"):
        st.markdown(a["answer"])
    if a.get("sql"):
        with st.expander("SQL" + (" (repaired once)" if a.get("repaired") else ""), expanded=True):
            st.code(a["sql"], language="sql")
            if a.get("executed_sql") and a["executed_sql"].strip() != a["sql"].strip():
                st.caption("Executed after validation (limit enforced, regenerated from the parsed query):")
                st.code(a["executed_sql"], language="sql")
            elif status_ == "blocked":
                st.caption("This statement was never sent to the database.")
            if a.get("assumptions"):
                st.caption(f"Assumption: {a['assumptions']}")
    if a.get("rows"):
        st.dataframe(pd.DataFrame(a["rows"], columns=a["columns"]), use_container_width=True, hide_index=True)
        if a.get("truncated"):
            st.caption("Row cap reached: showing the first rows only.")
        if a.get("chart"):
            render_chart(a["chart"], a["columns"], a["rows"])
    usage = a.get("usage") or {}
    if usage:
        steps = " → ".join(f"{s['name']} {s['ms'] / 1000:.1f}s" for s in a.get("steps", []))
        st.caption(f"{usage.get('total_ms', 0) / 1000:.1f}s total · {usage.get('llm_calls', 0)} model calls · "
                   f"${usage.get('cost_usd', 0):.4f}" + (f" · {steps}" if steps else ""))


def ask(question: str):
    st.session_state.messages.append({"role": "user", "content": question})
    history = [{"role": m["role"], "content": m["content"] if m["role"] == "user" else m.get("summary", "")}
               for m in st.session_state.messages[:-1] if m["role"] == "user" or m.get("summary")]
    resp = api("POST", "/api/ask", json={"question": question, "history": history[-6:]})
    st.session_state.messages.append({"role": "assistant", "content": "", "data": resp,
                                      "summary": resp.get("answer") or resp.get("reason", "")})


if "messages" not in st.session_state:
    st.session_state.messages = []

st.title("Askbase")
st.caption("Ask your database in plain English. Read-only by design, and the SQL is always shown.")
info = status()

with st.sidebar:
    st.subheader("Try a question")
    for q in info.get("examples", []):
        if st.button(q, key=f"ex-{q}", use_container_width=True):
            ask(q)
    st.subheader("Try an attack")
    st.caption("Destructive or sneaky prompts. They are stopped before the database sees them.")
    for q in info.get("attack_prompts", []):
        if st.button(q, key=f"at-{q}", use_container_width=True):
            ask(q)
    st.divider()
    st.subheader("SQL guardrail playground")
    sql = st.text_area("Paste any SQL (it is only checked, never run)", value="DROP TABLE orders;", height=90, key="pg")
    if st.button("Check this SQL", use_container_width=True):
        verdict = api("POST", "/api/validate", json={"sql": sql})
        if verdict.get("ok"):
            st.success("Allowed: read-only SELECT on allow-listed tables")
            st.code(verdict["sql"], language="sql")
        else:
            st.error("Blocked: " + "; ".join(verdict.get("reasons", [verdict.get("reason", "?")])))
    st.divider()
    if info.get("answer_mode") == "recorded":
        st.info("**Recorded-answer mode.** The suggested questions above were run by the real model and recorded. "
                "This demo replays them, so it never calls a paid API and cannot spend anyone's credits. "
                "Other questions are not answered here.")
    elif info.get("llm_configured") is False:
        st.warning("No language model is configured on this server, so questions are not answered yet. The guardrail playground still works.")
    st.caption(f"Row cap {info.get('max_rows', '?')} · timeout {info.get('statement_timeout_ms', 0) / 1000:.0f}s · "
               f"models: {info.get('models', {}).get('sql', '?')} / {info.get('models', {}).get('fast', '?')}")
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

if not st.session_state.messages:
    c1, c2, c3 = st.columns(3)
    c1.markdown("#### 1. Ask")
    c1.write("In plain English. Follow-up questions remember the conversation, so you can say *and for Germany?*")
    c2.markdown("#### 2. See the SQL")
    c2.write("Every answer shows the exact query next to a table and an automatic chart, so you can check it.")
    c3.markdown("#### 3. Stay safe")
    c3.write("Each query is parsed and validated, then run as a database role that cannot write. "
             "Try an attack from the sidebar.")
    st.markdown("**Sample data:** an online shop with customers, orders, order lines, products, categories, payments, "
                "shipments and reviews (January 2025 to mid-November 2026).")
    if info.get("answer_mode") == "recorded":
        st.info("Click one of the suggested questions or attacks in the sidebar. Their answers were recorded from the real model and "
                "are replayed against the live database, so nothing here spends API credits.")
    elif info.get("llm_configured") is False:
        st.info("This server has no language model configured yet, so questions are not answered. "
                "The SQL guardrail playground in the sidebar works and shows what would be stopped.")

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        if m["role"] == "user":
            st.markdown(m["content"])
        else:
            render_answer(m["data"])

if prompt := st.chat_input("Ask a question about orders, customers, products, payments…"):
    ask(prompt)
    st.rerun()
