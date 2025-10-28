import os
import json
import requests
from google.cloud import firestore
import vertexai
from vertexai.generative_models import GenerativeModel

# We will need a GitHub Token to post comments
# This should be passed as an env var
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

def format_report_body(data):
    """Helper to create a clean report body for Gemini."""
    body = "Please synthesize the following reports into a single, user-friendly, clean markdown comment.\n\n"
    
    if data.get("quality_status") == "complete":
        body += "--- Quality Report ---\n"
        for item in data.get("quality_analysis_results", []):
            body += f"File: {item['file_path']}\nFeedback: {item['feedback']}\n\n"
            
    if data.get("security_status") == "complete":
        body += "--- Security Report ---\n"
        for item in data.get("security_analysis_results", []):
            body += f"File: {item['file_path']}\nFeedback: {item['feedback']}\n\n"

    if data.get("docs_status") == "complete":
        body += "--- Documentation Report ---\n"
        for item in data.get("docs_analysis_results", []):
            body += f"File: {item['file_path']}\nFeedback: {item['feedback']}\n\n"
            
    return body

def post_to_github(pr_info, report_markdown):
    """Posts the final report as a comment on the PR."""
    pr_number = pr_info.get("pr_number")
    repo_full_name = pr_info.get("repo_full_name")
    
    # The GitHub API endpoint for issue comments (PRs are issues)
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    body = {
        "body": report_markdown
    }
    
    response = requests.post(url, headers=headers, json=body)
    
    if response.status_code == 201:
        print("Successfully posted comment to GitHub.")
    else:
        print(f"Error posting to GitHub: {response.status_code} - {response.text}")
        raise Exception(f"GitHub API Error: {response.text}")

def main():
    payload_str = os.environ.get("TASK_PAYLOAD")
    if not payload_str:
        print("Error: TASK_PAYLOAD environment variable not set.")
        return
    
    print(f"Payload string received: '{payload_str}'")
    task_payload = json.loads(payload_str)
    review_id = task_payload["review_id"]
    pr_info = task_payload["pr_info"]
    full_data = task_payload["full_data"]
    
    print(f"Starting CONSOLIDATION for review: {review_id}")

    # --- Initialize GCP Clients ---
    GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
    GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
    db = firestore.Client(project=GCP_PROJECT_ID)
    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    # Use a more powerful model for the final synthesis
    model = GenerativeModel("gemini-1.5-pro-001") 

    review_ref = db.collection("reviews").document(review_id)

    try:
        # 1. Create the prompt for Gemini
        synthesis_prompt = format_report_body(full_data)
        
        final_prompt = f"""
        You are a friendly and helpful AI code review co-pilot.
        Your job is to synthesize all the feedback from your specialist agents into a single, clean, and encouraging Markdown comment for a pull request.
        
        Start with a friendly opening, then present the findings grouped by category (Quality, Security, Documentation).
        Use markdown formatting, bullet points, and code blocks for clarity.
        End with a friendly closing.
        
        Here is the raw data:
        {synthesis_prompt}
        """
        
        # 2. Generate the report
        response = model.generate_content([final_prompt])
        final_report_markdown = response.text
        
        # 3. Post the report to GitHub
        post_to_github(pr_info, final_report_markdown)
        
        # 4. Mark the entire review as complete
        review_ref.update({"status": "complete", "final_report": final_report_markdown})
        
        print(f"Successfully completed CONSOLIDATION for {review_id}")

    except Exception as e:
        print(f"Error processing {review_id}: {e}")
        review_ref.update({"status": "error", "error_message": str(e)})

if __name__ == "__main__":
    main()
