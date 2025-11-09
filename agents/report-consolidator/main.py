# report-consolidator/main.py
import os
import json
import requests
import traceback

# --- NEW IMPORTS ---
import time
import random
from google.api_core import exceptions as api_exceptions
# --- END NEW IMPORTS ---

from google.cloud import firestore
import vertexai
from vertexai.generative_models import GenerativeModel

# --- Configuration / env ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# --- Initialize GCP Clients (Globally) ---
db = firestore.Client(project=GCP_PROJECT_ID)
vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
model = GenerativeModel("gemini-2.5-flash") # Using flash for consistency
transaction = db.transaction()


@firestore.transactional
def update_final_report_atomically(transaction, review_ref, task_sha, report_markdown):
    """
    Atomically update the review doc with the final report
    only if the head_sha matches task_sha.
    """
    doc_snapshot = review_ref.get(transaction=transaction)
    current_pr_info = doc_snapshot.get("pr_info") or {}
    current_sha = current_pr_info.get("head_sha")

    if current_sha == task_sha:
        print(f"SHA match ({task_sha}). Posting final report.")
        transaction.update(review_ref, {
            "status": "complete", # Use the main status field
            "final_report": report_markdown
        })
    else:
        print(f"Stale task. SHA mismatch (Task: {task_sha}, Doc: {current_sha}). Aborting final update.")

@firestore.transactional
def update_final_error_atomically(transaction, review_ref, task_sha, error_message):
    """
    Atomically update the review doc with the final error
    only if the head_sha matches task_sha.
    """
    doc_snapshot = review_ref.get(transaction=transaction)
    current_pr_info = doc_snapshot.get("pr_info") or {}
    current_sha = current_pr_info.get("head_sha")

    if current_sha == task_sha:
        print(f"SHA match ({task_sha}). Posting final error.")
        transaction.update(review_ref, {
            "status": "error", # Use the main status field
            "final_consolidator_error": str(error_message) # Use a specific error field
        })
    else:
        print(f"Stale error task. SHA mismatch (Task: {task_sha}, Doc: {current_sha}).")


def format_report_body(data):
    """
    Helper to create a clean report body for Gemini,
    now including error states from failed agents.
    """
    body = "Please synthesize the following reports into a single, user-friendly, clean markdown comment.\n\n"

    # --- Quality ---
    if data.get("quality_status") == "complete":
        body += "--- Quality Report ---\n"
        for item in data.get("quality_analysis_results", []):
            body += f"File: {item['file_path']}\nFeedback: {item['feedback']}\n\n"
    elif data.get("quality_status") == "error":
        body += "--- Quality Report (FAILED) ---\n"
        body += f"Error: {data.get('quality_error', 'Unknown error')}\n\n"

    # --- Security ---
    if data.get("security_status") == "complete":
        body += "--- Security Report ---\n"
        for item in data.get("security_analysis_results", []):
            body += f"File: {item['file_path']}\nFeedback: {item['feedback']}\n\n"
    elif data.get("security_status") == "error":
        body += "--- Security Report (FAILED) ---\n"
        body += f"Error: {data.get('security_error', 'Unknown error')}\n\n"

    # --- Docs ---
    if data.get("docs_status") == "complete":
        body += "--- Documentation Report ---\n"
        for item in data.get("docs_analysis_results", []):
            body += f"File: {item['file_path']}\nFeedback: {item['feedback']}\n\n"
    elif data.get("docs_status") == "error":
        body += "--- Documentation Report (FAILED) ---\n"
        body += f"Error: {data.get('docs_error', 'Unknown error')}\n\n"

    return body


def post_to_github(pr_info, report_markdown):
    """Posts the final report as a comment on the PR."""
    pr_number = pr_info.get("pr_number")
    repo_full_name = pr_info.get("repo_full_name")

    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    body = {"body": report_markdown}

    response = requests.post(url, headers=headers, json=body)

    if response.status_code == 201:
        print("Successfully posted comment to GitHub.")
    else:
        print(f"Error posting to GitHub: {response.status_code} - {response.text}")
        raise Exception(f"GitHub API Error: {response.text}")

# --- NEW HELPER FUNCTION ---
def generate_content_with_retry(model, prompt, max_retries=3):
    """Calls model.generate_content with exponential backoff for 429 errors."""
    retries = 0
    while retries < max_retries:
        try:
            # Send the request
            response = model.generate_content([prompt])
            # If successful, return the response
            return response
        except (
            api_exceptions.ResourceExhausted,  # 429
            api_exceptions.ServiceUnavailable, # 503
            api_exceptions.InternalServerError # 500
        ) as e:
            # Catch specific retryable API errors
            retries += 1
            if retries >= max_retries:
                print(f"[ERROR] Max retries reached. Model call failed: {e}")
                raise e # Re-raise the last exception
            
            # Exponential backoff with jitter: 2^retries + random_fraction
            wait_time = (2 ** retries) + random.random()
            print(f"[WARN] Model API retryable error ({e.__class__.__name__}): Retrying in {wait_time:.2f}s... ({retries}/{max_retries})")
            time.sleep(wait_time)
        except Exception as e:
            # Catch any other non-retryable error (like a 400 Bad Request)
            print(f"[ERROR] Model call failed with non-retryable error: {e}")
            raise e # Re-raise immediately
# --- END NEW HELPER FUNCTION ---


def main():
    # --- Robust payload handling ---
    payload_str = os.environ.get("TASK_PAYLOAD", "").strip()
    if not payload_str:
        print("Error: TASK_PAYLOAD environment variable not set or empty.")
        return

    try:
        task_payload = json.loads(payload_str)
    except json.JSONDecodeError:
        print("Warning: TASK_PAYLOAD not valid JSON. Ignoring.")
        return

    # --- Handle simple triggers gracefully ---
    if "review_id" not in task_payload or "pr_info" not in task_payload:
        print(f"Received simple trigger or invalid payload: {task_payload}")
        # Nothing to process — exit gracefully instead of crashing
        return

    review_id = task_payload["review_id"]
    pr_info = task_payload["pr_info"]
    full_data = task_payload.get("full_data", {}) # Get full_data, default to empty
    
    # --- CRITICAL: Get task_sha for atomic operations ---
    task_sha = pr_info.get("head_sha")
    if not task_sha:
        print(f"Error: Payload for {review_id} is missing 'pr_info.head_sha'. Aborting.")
        return

    print(f"Starting CONSOLIDATION for review: {review_id} (SHA: {task_sha})")

    review_ref = db.collection("reviews").document(review_id)

    try:
        synthesis_prompt = format_report_body(full_data)

        final_prompt = f"""
        You are a friendly and helpful AI code review co-pilot.
        Your job is to synthesize all the feedback from your specialist agents into a single, clean, and encouraging Markdown comment for a pull request.

        Start with a friendly opening (e.g., "Hi team, I've taken a look at the latest changes...").
        Then, present the findings grouped by category (e.g., ## 🤖 Quality Scan, ## 🔒 Security Scan, ## 📚 Documentation Scan).
        If a scan failed, state that it failed and present the error.
        If a scan passed with no feedback, just say "Looks good!" or "No issues found."

        Use markdown formatting, bullet points, and code blocks for clarity.
        End with a friendly closing (e.g., "Keep up the great work!").

        Here is the raw data:
        {synthesis_prompt}
        """

        # Generate the report (with retry)
        response = generate_content_with_retry(model, final_prompt)
        final_report_markdown = response.text

        # Post to GitHub
        post_to_github(pr_info, final_report_markdown)

        # Mark Firestore document complete (atomically)
        update_final_report_atomically(transaction, review_ref, task_sha, final_report_markdown)
        print(f"Successfully completed CONSOLIDATION for {review_id}")

    except Exception as e:
        tb = traceback.format_exc()
        print(f"Error processing {review_id}: {e}\n{tb}")
        # Update error atomically
        update_final_error_atomically(transaction, review_ref, task_sha, f"{e}\n{tb}")


if __name__ == "__main__":
    main()