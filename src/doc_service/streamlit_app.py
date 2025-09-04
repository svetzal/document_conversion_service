import os
import time
import io
import requests
import streamlit as st

API_BASE = os.getenv("DOC_SERVICE_API_BASE", os.getenv("API_BASE", "http://localhost:8080")).rstrip("/")

def _reset_state():
    for key in [
        "job_id",
        "token",
        "status",
        "progress",
        "result_text",
        "error",
    ]:
        if key in st.session_state:
            del st.session_state[key]


def _start_job(uploaded_file: io.BytesIO) -> tuple[str, str] | None:
    try:
        files = {"file": (uploaded_file.name, uploaded_file.getvalue(), uploaded_file.type or "application/octet-stream")}
        resp = requests.post(f"{API_BASE}/jobs", files=files, timeout=60)
    except Exception as e:
        st.session_state["error"] = f"Failed to connect to API: {e}"
        return None
    if resp.status_code not in (200, 202):
        st.session_state["error"] = f"Upload failed: {resp.status_code} {resp.text}"
        return None
    data = resp.json()
    job_id = str(data.get("id"))
    token = str(data.get("access_token"))
    return job_id, token


def _poll_status(job_id: str, token: str) -> dict[str, object] | None:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(f"{API_BASE}/jobs/{job_id}", headers=headers, timeout=30)
    except Exception as e:
        st.session_state["error"] = f"Status check failed: {e}"
        return None
    if resp.status_code != 200:
        st.session_state["error"] = f"Status error: {resp.status_code} {resp.text}"
        return None
    return resp.json()


def _download_result(job_id: str, token: str) -> str | None:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(f"{API_BASE}/jobs/{job_id}/result", headers=headers, timeout=60)
    except Exception as e:
        st.session_state["error"] = f"Download failed: {e}"
        return None
    if resp.status_code != 200:
        st.session_state["error"] = f"Download error: {resp.status_code} {resp.text}"
        return None
    return resp.text


def main() -> None:
    st.set_page_config(page_title="Document Conversion Service", page_icon="📄", layout="centered")
    st.title("📄 Document Conversion Service")
    st.caption(f"API base: {API_BASE}")

    # Restart button at the top
    col1, col2 = st.columns([1,1])
    with col1:
        if st.button("Restart", type="secondary"):
            _reset_state()
            st.rerun()
    with col2:
        st.write("")

    # Upload section
    uploaded = st.file_uploader("Upload a document (PDF, DOCX, PPTX, etc.)", type=["pdf","docx","pptx","ppsx","ppt","xlsx"])  # type: ignore[arg-type]

    # Start job
    if uploaded and "job_id" not in st.session_state and st.button("Start Conversion", type="primary"):
        with st.spinner("Uploading and creating job..."):
            res = _start_job(uploaded)
        if res:
            job_id, token = res
            st.session_state["job_id"] = job_id
            st.session_state["token"] = token
            st.session_state["status"] = "queued"
            st.session_state["progress"] = 0
            st.toast("Job created", icon="✅")
        else:
            st.error(st.session_state.get("error", "Unknown error"))

    # Show status and poll if job exists
    if "job_id" in st.session_state and "token" in st.session_state:
        job_id = st.session_state["job_id"]
        token = st.session_state["token"]
        with st.status("Tracking job status...", expanded=True) as status_box:
            while True:
                data = _poll_status(job_id, token)
                if not data:
                    st.error(st.session_state.get("error", "Status error"))
                    break
                st.session_state["status"] = str(data.get("status", "unknown"))
                st.session_state["progress"] = int(data.get("progress", 0))

                st.write(f"Status: {st.session_state['status']}")
                st.progress(min(max(st.session_state["progress"], 0), 100))

                if st.session_state["status"] in {"succeeded", "completed", "done"}:
                    status_box.update(label="Job completed", state="complete")
                    break
                if st.session_state["status"] in {"failed", "error"}:
                    status_box.update(label="Job failed", state="error")
                    break
                time.sleep(1.5)

        # On completion, try to fetch result
        if st.session_state.get("status") in {"succeeded", "completed", "done"}:
            with st.spinner("Fetching result..."):
                text = _download_result(job_id, token)
            if text is not None:
                st.session_state["result_text"] = text

    # Show result download and preview
    if "result_text" in st.session_state:
        st.success("Conversion complete!")
        md = st.session_state["result_text"]
        st.download_button(
            label="Download Markdown",
            data=md.encode("utf-8"),
            file_name="conversion.md",
            mime="text/markdown",
        )
        with st.expander("Preview"):
            st.markdown(md)

    # Error display
    if err := st.session_state.get("error"):
        st.error(err)


if __name__ == "__main__":
    main()
