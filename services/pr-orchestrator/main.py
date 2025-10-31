import os
import hmac
import hashlib
import json
from fastapi import FastAPI, Request, HTTPException, Response
import uvicorn
from google.cloud import firestore
from google.cloud import pubsub_v1

# --- Configuration from environment variables ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode("utf-8")

# Pub/Sub topic IDs
QUALITY_TOPIC_ID = "code-review-tasks"
SECURITY_TOPIC_ID = "security-review-tasks"
DOCS_TOPIC_ID = "docs-review-tasks"

# --- Initialize clients ---
app = FastAPI()
db = firestore.AsyncClient(project=GCP_PROJECT_ID)
publisher = pubsub_v1.PublisherClient()

quality_topic_path = publisher.topic_path(GCP_PROJECT_ID, QUALITY_TOPIC_ID)
security_topic_path = publisher.topic_path(GCP_PROJECT_ID, SECURITY_TOPIC_ID)
docs_topic_path = publisher.topic_path(GCP_PROJECT_ID, DOCS_TOPIC_ID)

# --- Helper function to verify GitHub signature ---
def verify_signature(request_body: bytes, signature: str):
    if not signature:
        raise HTTPException(status_code=403, detail="Signature missing")
    try:
        sha_name, signature_hash = signature.split("=", 1)
    except Exception:
        raise HTTPException(status_code=403, detail="Malformed signature header")
    if sha_name != "sha256":
        raise HTTPException(status_code=501, detail="Unsupported signature algorithm")
    mac = hmac.new(GITHUB_WEBHOOK_SECRET, msg=request_body, digestmod=hashlib.sha256)
    if not hmac.compare_digest(mac.hexdigest(), signature_hash):
        raise HTTPException(status_code=403, detail="Invalid signature")

# --- Webhook endpoint ---
@app.post("/webhook")
async def receive_webhook(request: Request):
    # 1. Verify GitHub signature
    signature = request.headers.get("X-Hub-Signature-256")
    request_body = await request.body()
    try:
        # If secret is empty, signature comparison will still run (but fail) unless webhook was configured without secret.
        verify_signature(request_body, signature)
    except HTTPException as e:
        print(f"Signature verification failed: {e.detail}")
        raise e

    # 2. Process only relevant pull request actions
    payload = await request.json()
    action = payload.get("action")
    if action not in ["opened", "reopened", "synchronize"]:
        print(f"Ignoring action: {action}")
        return Response(status_code=204)  # No content

    # 3. Extract key PR information (include base_sha/head_ref/base_ref)
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    try:
        pr_info = {
            "pr_number": payload.get("number"),
            "repo_full_name": repo.get("full_name"),
            "html_url": pr.get("html_url"),
            "head_sha": pr.get("head", {}).get("sha"),
            "base_sha": pr.get("base", {}).get("sha"),
            # optional convenience fields
            "head_ref": pr.get("head", {}).get("ref"),
            "base_ref": pr.get("base", {}).get("ref"),
        }
    except Exception as e:
        print(f"[ERROR] Failed to extract pr_info: {e}")
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    # 4. Create/update Firestore document for state tracking
    review_id = f"{pr_info['repo_full_name'].replace('/', '_')}_{pr_info['pr_number']}"
    review_ref = db.collection("reviews").document(review_id)

    print(f"Creating/updating review document: {review_id}")
    # Write minimal state to Firestore so workers can check head_sha/base_sha
    await review_ref.set({
        "status": "pending",
        "pr_info": pr_info,
        "created_at": firestore.SERVER_TIMESTAMP,
        "tasks_completed": 0,
        "total_tasks": 3,
        "quality_status": "pending",
        "security_status": "pending",
        "docs_status": "pending"
    }, merge=True)

    # 5. Prepare message payload
    message_data = json.dumps({
        "review_id": review_id,
        "pr_info": pr_info
    }).encode("utf-8")

    # 6/7/8. Publish to review topics
    try:
        future_quality = publisher.publish(quality_topic_path, data=message_data)
        print(f"Published message {future_quality.result()} for review {review_id} to {QUALITY_TOPIC_ID}")

        future_security = publisher.publish(security_topic_path, data=message_data)
        print(f"Published message {future_security.result()} for review {review_id} to {SECURITY_TOPIC_ID}")

        future_docs = publisher.publish(docs_topic_path, data=message_data)
        print(f"Published message {future_docs.result()} for review {review_id} to {DOCS_TOPIC_ID}")
    except Exception as e:
        print(f"[ERROR] Failed to publish to Pub/Sub topics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "success", "review_id": review_id}

# --- Run server ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
