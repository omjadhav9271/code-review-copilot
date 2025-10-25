import os
import hmac
import hashlib
import json
from fastapi import FastAPI, Request, HTTPException, Response
import uvicorn
from google.cloud import firestore
from google.cloud import pubsub_v1

# Configuration from environment variables
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode("utf-8")
PUBSUB_TOPIC_ID = "code-review-tasks" # The topic we just created

# Initialize clients
app = FastAPI()
db = firestore.AsyncClient(project=GCP_PROJECT_ID)
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(GCP_PROJECT_ID, PUBSUB_TOPIC_ID)

def verify_signature(request_body: bytes, signature: str):
    """Verify that the request is from GitHub."""
    if not signature:
        raise HTTPException(status_code=403, detail="Signature missing")
    
    sha_name, signature_hash = signature.split("=", 1)
    if sha_name != "sha256":
        raise HTTPException(status_code=501, detail="Unsupported signature algorithm")

    mac = hmac.new(GITHUB_WEBHOOK_SECRET, msg=request_body, digestmod=hashlib.sha256)
    
    if not hmac.compare_digest(mac.hexdigest(), signature_hash):
        raise HTTPException(status_code=403, detail="Invalid signature")

@app.post("/webhook")
async def receive_webhook(request: Request):
    # 1. Verify the incoming request is from GitHub
    signature = request.headers.get("X-Hub-Signature-256")
    request_body = await request.body()
    try:
        verify_signature(request_body, signature)
    except HTTPException as e:
        print(f"Signature verification failed: {e.detail}")
        raise e

    # 2. Process only relevant pull request actions
    payload = await request.json()
    if payload.get("action") not in ["opened", "reopened", "synchronize"]:
        print(f"Ignoring action: {payload.get('action')}")
        return Response(status_code=204) # No content, successful but no action needed

    # 3. Extract key information
    pr_info = {
        "pr_number": payload["number"],
        "repo_full_name": payload["repository"]["full_name"],
        "html_url": payload["pull_request"]["html_url"],
        "head_sha": payload["pull_request"]["head"]["sha"],
    }
    
    # 4. Create a state tracking document in Firestore
    review_id = f"{pr_info['repo_full_name'].replace('/', '_')}_{pr_info['pr_number']}"
    review_ref = db.collection("reviews").document(review_id)
    
    print(f"Creating/updating review document: {review_id}")
    await review_ref.set({
        "status": "pending", # Overall status
        "pr_info": pr_info,
        "created_at": firestore.SERVER_TIMESTAMP,
        "tasks_completed": 0,
        "total_tasks": 3,
        "quality_status": "pending",  # <-- REQUIRED FIELD
        "security_status": "pending", # <-- FOR FUTURE AGENT
        "docs_status": "pending"      # <-- FOR FUTURE AGENT
    }, merge=True) # Use merge=True to not overwrite existing results on 'synchronize'

    # 5. Dispatch task message to Pub/Sub
    message_data = json.dumps({
        "review_id": review_id,
        "pr_info": pr_info # Pass the full info
    }).encode("utf-8")
    
    future = publisher.publish(topic_path, data=message_data)
    print(f"Published message {future.result()} for review {review_id}")
    return {"status": "success", "review_id": review_id}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
