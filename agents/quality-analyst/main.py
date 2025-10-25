import os
import json
import tempfile
import git

from google.cloud import firestore
import vertexai
from vertexai.generative_models import GenerativeModel

def main():
    """
    Main entry point for the Job. Reads its task from an env var.
    """
    # 1. Read the task payload from an environment variable
    payload_str = os.environ.get("TASK_PAYLOAD")
    if not payload_str:
        print("Error: TASK_PAYLOAD environment variable not set. Exiting.")
        return

    print(f"Received payload: {payload_str}")
    task_payload = json.loads(payload_str)
    
    review_id = task_payload["review_id"]
    pr_info = task_payload["pr_info"]
    repo_full_name = pr_info["repo_full_name"]
    head_sha = pr_info["head_sha"]
    
    print(f"Starting quality analysis for review: {review_id}")

    # --- Initialize GCP Clients ---
    GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
    GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
    
    db = firestore.Client(project=GCP_PROJECT_ID)
    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    model = GenerativeModel("gemini-1.5-flash-001") 

    review_ref = db.collection("reviews").document(review_id)

    try:
        # 2. Clone the repository
        repo_url = f"https://github.com/{repo_full_name}.git"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = git.Repo.clone_from(repo_url, tmpdir)
            repo.git.checkout(head_sha)
            
            # 3. Identify changed files (simplified)
            changed_files = [item.a_path for item in repo.index.diff(None) if item.a_path.endswith(('.py', '.js', '.go'))]
            
            if not changed_files:
                print(f"No relevant files changed for {review_id}.")
                review_ref.update({
                    "quality_analysis_results": [{"file_path": "N/A", "feedback": "No relevant files (.py, .js, .go) were changed."}],
                    "quality_status": "complete",
                    "tasks_completed": firestore.Increment(1)
                })
                return

            # 4. Analyze each changed file with Gemini
            analysis_results = []
            for file_path in changed_files:
                full_path = os.path.join(tmpdir, file_path)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        file_content = f.read()
                except (IOError, UnicodeDecodeError) as e:
                    print(f"Could not read file {file_path}: {e}")
                    analysis_results.append({"file_path": file_path, "feedback": "Error: Could not read file."})
                    continue
                
                prompt = f"""
                You are an expert code reviewer. Analyze the following code snippet from the file '{file_path}' for quality issues.
                Focus on:
                - Code smells (e.g., long methods, duplicate code).
                - Potential bugs or logical errors.
                - Non-adherence to common best practices.
                - Lack of clarity or readability.

                Provide your feedback as a list of bullet points. If there are no issues, respond with "No issues found."

                CODE:
                ```
                {file_content}
                ```
                """
                
                response = model.generate_content([prompt])
                analysis_results.append({
                    "file_path": file_path,
                    "feedback": response.text.strip()
                })

        # 5. Update Firestore
        review_ref.update({
            "quality_analysis_results": analysis_results,
            "quality_status": "complete",
            "tasks_completed": firestore.Increment(1)
        })
        print(f"Successfully completed quality analysis for {review_id}")

    except Exception as e:
        print(f"Error processing {review_id}: {e}")
        review_ref.update({"quality_status": "error", "error_message": str(e), "quality_status": "error"})

if __name__ == "__main__":
    main()
