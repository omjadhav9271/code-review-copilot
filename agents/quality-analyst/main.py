import os
import json
import tempfile
import git

from google.cloud import firestore
import vertexai
from vertexai.generative_models import GenerativeModel

# --- Initialize GCP Clients ---
# Initializing the client here is fine
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
db = firestore.Client(project=GCP_PROJECT_ID)
vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
model = GenerativeModel("gemini-1.5-flash-001")

# We create a transaction object
transaction = db.transaction()

@firestore.transactional
def update_firestore_atomically(transaction, review_ref, task_sha, analysis_results):
    """
    This function runs as an atomic transaction.
    It will only update if the SHA matches.
    """
    doc_snapshot = review_ref.get(transaction=transaction)
    current_pr_info = doc_snapshot.get("pr_info")
    current_sha = current_pr_info.get("head_sha")

    if current_sha == task_sha:
        # The SHA matches! This is not a stale task.
        print(f"SHA match ({task_sha}). Updating Firestore.")
        transaction.update(review_ref, {
            "quality_analysis_results": analysis_results,
            "quality_status": "complete",
            "tasks_completed": firestore.Increment(1)
        })
    else:
        # The SHA does not match. This is a stale task.
        print(f"Stale task. SHA mismatch (Task: {task_sha}, Doc: {current_sha}). Aborting update.")

def main():
    """
    Main entry point for the Job.
    """
    payload_str = os.environ.get("TASK_PAYLOAD")
    if not payload_str:
        print("Error: TASK_PAYLOAD environment variable not set. Exiting.")
        return

    task_payload = json.loads(payload_str)
    
    review_id = task_payload["review_id"]
    pr_info = task_payload["pr_info"]
    
    # This is the SHA this task is assigned to
    task_sha = pr_info["head_sha"] 
    
    print(f"Starting quality analysis for review: {review_id} (SHA: {task_sha})")
    
    review_ref = db.collection("reviews").document(review_id)
    analysis_results = [] # Store results here

    try:
        # --- 1. Perform all slow analysis *outside* the transaction ---
        repo_url = f"https://github.com/{pr_info['repo_full_name']}.git"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = git.Repo.clone_from(repo_url, tmpdir)
            repo.git.checkout(task_sha)
            
            changed_files = [item.a_path for item in repo.index.diff(None) if item.a_path.endswith(('.py', '.js', '.go'))]
            
            if not changed_files:
                analysis_results = [{"file_path": "N/A", "feedback": "No relevant files (.py, .js, .go) were changed."}]
            else:
                for file_path in changed_files:
                    # ... (file reading logic) ...
                    try:
                        with open(os.path.join(tmpdir, file_path), "r", encoding="utf-8") as f:
                            file_content = f.read()
                    except Exception:
                        continue 

                    prompt = f"..." # Your Gemini prompt
                    response = model.generate_content([prompt])
                    analysis_results.append({
                        "file_path": file_path,
                        "feedback": response.text.strip()
                    })

        # --- 2. Run the atomic update ---
        update_firestore_atomically(transaction, review_ref, task_sha, analysis_results)
        print(f"Successfully completed quality analysis for {review_id}")

    except Exception as e:
        print(f"Error processing {review_id}: {e}")
        # Update with error, but check SHA first to avoid stale errors
        @firestore.transactional
        def update_error_atomically(transaction, review_ref, task_sha, error_message):
            doc = review_ref.get(transaction=transaction)
            if doc.get("pr_info.head_sha") == task_sha:
                transaction.update(review_ref, {"quality_status": "error", "error_message": str(error_message)})
        
        try:
            update_error_atomically(transaction, review_ref, task_sha, str(e))
        except Exception as tx_error:
            print(f"Failed to write error state: {tx_error}")

if __name__ == "__main__":
    main()