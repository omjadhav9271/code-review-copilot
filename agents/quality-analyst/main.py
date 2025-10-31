# quality-analyst/main.py
import os
import json
import base64
import tempfile
import traceback
from typing import List, Optional, Dict, Any

import requests
import git
from google.cloud import firestore
import vertexai
from vertexai.generative_models import GenerativeModel

# --- Configuration / env ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # optional but recommended for private repos / higher rate limits
GITHUB_API = "https://api.github.com"
# If set to "1" or "true" (case-insensitive) we will allow falling back to cloning the repo when GitHub API fails.
ALLOW_CLONE_FALLBACK = os.environ.get("ALLOW_CLONE_FALLBACK", "false").lower() in ("1", "true")
# Skip files larger than this many bytes when fetching from API or git blob
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", 1024 * 1024))  # 1 MB default

# --- Initialize GCP clients and VertexAI ---
db = firestore.Client(project=GCP_PROJECT_ID)
vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
model = GenerativeModel("gemini-1.0-pro")

# Pre-create a transaction object to pass into @firestore.transactional functions
transaction = db.transaction()

# ---------------- GitHub helpers ----------------
def _github_headers(token: Optional[str]):
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "code-review-copilot"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers

def github_api_get(url: str, token: Optional[str] = None, timeout: int = 15) -> Any:
    resp = requests.get(url, headers=_github_headers(token), timeout=timeout)
    resp.raise_for_status()
    return resp.json()

def fetch_changed_files_from_github(repo_full_name: str, pr_number: int, token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return list of file dicts from /pulls/{pr_number}/files. Each dict contains at least 'filename', 'raw_url', 'sha', 'status'."""
    files = []
    page = 1
    while True:
        url = f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pr_number}/files?page={page}&per_page=100"
        page_files = github_api_get(url, token)
        if not page_files:
            break
        files.extend(page_files)
        if len(page_files) < 100:
            break
        page += 1
    return files

def fetch_file_content_from_github(repo_full_name: str, path: str, ref: str, token: Optional[str] = None) -> Optional[str]:
    """
    Uses the Contents API to fetch a file at given ref (sha or branch).
    Returns decoded text, or None for binary/unreadable files.
    """
    url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}?ref={ref}"
    data = github_api_get(url, token)
    # If it's a file, content is base64 encoded
    if isinstance(data, dict) and data.get("content"):
        encoding = data.get("encoding", "base64")
        if encoding != "base64":
            raise ValueError(f"Unexpected encoding {encoding}")
        raw = base64.b64decode(data["content"])
        if len(raw) > MAX_FILE_BYTES:
            print(f"[WARN] Skipping {path}: file too large ({len(raw)} bytes)")
            return None
        # try decode to utf-8; fallback with replace
        return raw.decode("utf-8", errors="replace")
    # directories or unexpected responses -> skip
    return None

# ---------------- Firestore transactional helpers ----------------
@firestore.transactional
def update_firestore_atomically(transaction, review_ref, task_sha, analysis_results):
    """
    Atomically update the review doc if the head_sha matches task_sha.
    """
    doc_snapshot = review_ref.get(transaction=transaction)
    current_pr_info = doc_snapshot.get("pr_info") or {}
    current_sha = current_pr_info.get("head_sha")
    if current_sha == task_sha:
        print(f"SHA match ({task_sha}). Updating Firestore.")
        transaction.update(review_ref, {
            "quality_analysis_results": analysis_results,
            "quality_status": "complete",
            "tasks_completed": firestore.Increment(1)
        })
    else:
        print(f"Stale task. SHA mismatch (Task: {task_sha}, Doc: {current_sha}). Aborting update.")

@firestore.transactional
def update_error_atomically(transaction, review_ref, task_sha, error_message):
    """
    Set error state only if the doc still refers to task_sha.
    """
    doc_snapshot = review_ref.get(transaction=transaction)
    current_pr_info = doc_snapshot.get("pr_info") or {}
    current_sha = current_pr_info.get("head_sha")
    if current_sha == task_sha:
        transaction.update(review_ref, {"quality_status": "error", "error_message": str(error_message)})

# ---------------- Git fallback helpers ----------------
def compute_changed_files_via_clone(repo_url: str, head_sha: str, base_sha: Optional[str]) -> List[str]:
    """
    Clone shallowly and compute changed files between base_sha and head_sha.
    Return list of file paths (strings).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # shallow clone but not single-branch to allow later fetches
        repo = git.Repo.clone_from(repo_url, tmpdir, depth=1, no_single_branch=True)
        # Try to fetch both SHAs
        def try_fetch(sha: str):
            try:
                repo.git.fetch("origin", sha)
            except Exception as e:
                print(f"[INFO] fetch origin {sha} failed: {e}")

        try_fetch(head_sha)
        if base_sha:
            try_fetch(base_sha)

        # Checkout head_sha (best-effort)
        try:
            repo.git.checkout(head_sha)
        except Exception:
            try:
                repo.git.checkout("FETCH_HEAD")
            except Exception:
                print(f"[WARN] Could not checkout {head_sha}; continuing.")

        # Compute diff name-only
        try:
            if base_sha:
                raw = repo.git.diff("--name-only", f"{base_sha}...{head_sha}")
            else:
                # fallback: list files in HEAD (not ideal)
                raw = repo.git.diff("--name-only", "HEAD~1..HEAD")
            files = [p.strip() for p in raw.splitlines() if p.strip()]
            return files
        except Exception as e:
            print(f"[ERROR] git diff failed: {e}")
            return []

def read_file_from_git(repo_url: str, sha: str, file_path: str) -> Optional[str]:
    """
    Clone minimal and use git show to get blob content for given sha:path.
    Uses a temp repo then repo.git.show(f"{sha}:{file_path}")
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = git.Repo.clone_from(repo_url, tmpdir, depth=1, no_single_branch=True)
        try:
            repo.git.fetch("origin", sha)
        except Exception:
            pass
        try:
            content = repo.git.show(f"{sha}:{file_path}")
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                print(f"[WARN] Skipping {file_path}: too large")
                return None
            return content
        except Exception as e:
            print(f"[WARN] git show failed for {file_path} at {sha}: {e}")
            return None

# ---------------- Main ----------------
def main():
    payload_str = os.environ.get("TASK_PAYLOAD")
    if not payload_str:
        print("Error: TASK_PAYLOAD not set.")
        return

    task_payload = json.loads(payload_str)
    review_id = task_payload["review_id"]
    pr_info = task_payload["pr_info"]
    pr_number = pr_info.get("pr_number")
    repo_full_name = pr_info.get("repo_full_name")
    task_sha = pr_info.get("head_sha")
    base_sha = pr_info.get("base_sha")  # may be None if orchestrator didn't set it
    head_ref = pr_info.get("head_ref")
    base_ref = pr_info.get("base_ref")

    print(f"Starting quality analysis for review {review_id} ({repo_full_name} PR #{pr_number}) SHA={task_sha}")

    review_ref = db.collection("reviews").document(review_id)
    analysis_results = []

    try:
        # --- Preferred path: GitHub REST API to list changed files & fetch contents ---
        use_api = True
        github_api_error = None
        changed_file_paths: List[str] = []

        if pr_number and repo_full_name:
            try:
                print("[INFO] Attempting to list changed files via GitHub API...")
                gh_files = fetch_changed_files_from_github(repo_full_name, pr_number, GITHUB_TOKEN)
                for f in gh_files:
                    filename = f.get("filename")
                    if filename and filename.endswith(('.py', '.js', '.go')):
                        changed_file_paths.append(filename)
                print(f"[INFO] GitHub API returned {len(changed_file_paths)} relevant files.")
            except Exception as e:
                github_api_error = str(e)
                print(f"[WARN] GitHub API file-list failed: {e}")
                use_api = False

        # If API produced no files and clone fallback allowed, do clone fallback
        if (not changed_file_paths) and (ALLOW_CLONE_FALLBACK or not use_api):
            if not ALLOW_CLONE_FALLBACK and not use_api:
                print("[WARN] GitHub API failed and clone fallback is disabled.")
            if ALLOW_CLONE_FALLBACK:
                try:
                    print("[INFO] Falling back to git clone approach to compute changed files...")
                    repo_url = f"https://github.com/{repo_full_name}.git"
                    changed_file_paths = compute_changed_files_via_clone(repo_url, task_sha, base_sha)
                    # filter by extensions
                    changed_file_paths = [p for p in changed_file_paths if p.endswith(('.py', '.js', '.go'))]
                    print(f"[INFO] Clone fallback returned {len(changed_file_paths)} relevant files.")
                except Exception as e:
                    print(f"[ERROR] Clone fallback failed: {e}")
                    changed_file_paths = []

        # If still no changed files, emit a helpful result and finish (no write if stale)
        if not changed_file_paths:
            analysis_results = [{"file_path": "N/A", "feedback": "No relevant files (.py, .js, .go) were changed."}]
            update_firestore_atomically(transaction, review_ref, task_sha, analysis_results)
            print(f"Completed (no files) for {review_id}")
            return

        # For each file, fetch content (prefer API content)
        for file_path in changed_file_paths:
            file_content = None
            # Try GitHub API content first (if available)
            if pr_number and repo_full_name and use_api:
                try:
                    file_content = fetch_file_content_from_github(repo_full_name, file_path, task_sha, GITHUB_TOKEN)
                except Exception as e:
                    print(f"[WARN] Failed to fetch {file_path} from GitHub API: {e}")

            # If API not available or returned None, try git show via clone fallback (on-demand)
            if file_content is None and ALLOW_CLONE_FALLBACK:
                try:
                    repo_url = f"https://github.com/{repo_full_name}.git"
                    file_content = read_file_from_git(repo_url, task_sha, file_path)
                except Exception as e:
                    print(f"[WARN] Failed to read {file_path} via git fallback: {e}")

            if not file_content:
                analysis_results.append({
                    "file_path": file_path,
                    "feedback": "Unable to retrieve file contents (possibly binary or too large)."
                })
                continue

            # --- Run the model analysis (Gemini) ---
            try:
                prompt = (
                    f"Perform quality code review of the file `{file_path}` in repository `{repo_full_name}`.\n"
                    f"PR: {pr_number} SHA: {task_sha}\n\n"
                    f"File contents:\n```\n{file_content}\n```\n\n"
                    "Provide concise, actionable feedback (bugs, style, complexity, security concerns, tests missing) "
                    "and a short severity score (low/medium/high)."
                )
                # Using model.generate_content per your prior code shape
                response = model.generate_content([prompt])
                # response may be a list or object depending on SDK; attempt robust access:
                feedback_text = None
                if hasattr(response, "text"):
                    feedback_text = response.text
                elif isinstance(response, (list, tuple)) and len(response) and hasattr(response[0], "text"):
                    feedback_text = response[0].text
                else:
                    # last-resort: convert to string
                    feedback_text = str(response)

                analysis_results.append({
                    "file_path": file_path,
                    "feedback": feedback_text.strip() if feedback_text else "No feedback from model."
                })
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[ERROR] Model call failed for {file_path}: {e}\n{tb}")
                analysis_results.append({
                    "file_path": file_path,
                    "feedback": f"Model analysis failed: {e}"
                })

        # --- Atomic write to Firestore if SHA still matches ---
        update_firestore_atomically(transaction, review_ref, task_sha, analysis_results)
        print(f"Successfully completed quality analysis for {review_id}")

    except Exception as e:
        print(f"[ERROR] Unhandled error while processing {review_id}: {e}")
        tb = traceback.format_exc()
        print(tb)
        try:
            update_error_atomically(transaction, review_ref, task_sha, f"{e}\n{tb}")
        except Exception as tx_error:
            print(f"[ERROR] Failed to write error state: {tx_error}")


if __name__ == "__main__":
    main()
