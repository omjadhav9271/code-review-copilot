import os
import hmac
import hashlib
import json
from fastapi import FastAPI, Request, HTTPException, Response

# Initialize GCP clients
from google.cloud import firestore
from google.cloud import pubsub_v1

# Configuration from environment variables
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode("utf-8")
PUBSUB_TOPIC_ID = "code-review-tasks"  # The topic we just created

# Initialize clients
app = FastAPI()
db = firestore.AsyncClient(project=GCP_PROJECT_ID)
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(GCP_PROJECT_ID, PUBSUB_TOPIC_ID)

def verify_signature(request_body: bytes, signature: str):
    """Verify that the request is from GitHub."""
    if not signature:
        raise HTTPException(status_code=403, detail="Signature missing")

    # Format the signature from "sha256=..." to just the hash
    sha_name, signature_hash = signature.split("=", 1)
    if sha_name != "sha256":
        raise HTTPException(status_code=501, detail="Unsupported signature algorithm")

    # Create our own signature
    mac = hmac.new(GITHUB_WEBHOOK_SECRET, msg=request_body, digestmod=hashlib.sha256)

    if not hmac.compare_digest(mac.hexdigest(), signature_hash):
        raise HTTPException(status_code=403, detail="Invalid signature")

@app.get("/")
def read_root():
    return {"Status": "PR Orchestrator is running"}

@app.post("/webhook")
async def receive_webhook(request: Request):
    # 1. Verify the incoming request is from GitHub
    signature = request.headers.get("X-Hub-Signature-256")
    request_body = await request.body()
    verify_signature(request_body, signature)

    # 2. Process only relevant pull request actions
    payload = await request.json()
    if payload.get("action") not in ["opened", "reopened", "synchronize"]:
        return Response(status_code=204)  # No content, successful but no action needed

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
    await review_ref.set({
        "status": "pending",
        "pr_info": pr_info,
        "created_at": firestore.SERVER_TIMESTAMP,
        "tasks_completed": 0,
        "total_tasks": 3  # Quality, Security, Docs
    })

    # 5. Dispatch task message to Pub/Sub
    message_data = json.dumps({
        "review_id": review_id,
        "pr_info": pr_info
    }).encode("utf-8")

    publisher.publish(topic_path, data=message_data)

    print(f"Successfully dispatched review task for {review_id}")
    return {"status": "success", "review_id": review_id}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)