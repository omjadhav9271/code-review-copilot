import os
import base64
from fastapi import FastAPI, Request, HTTPException, Response
import uvicorn
from google.cloud.run_v2 import JobsClient, RunJobRequest, EnvVar

# --- Configuration ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
TARGET_JOB_NAME = os.environ.get("TARGET_JOB_NAME", "quality-analyst")
TARGET_JOB_PATH = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/jobs/{TARGET_JOB_NAME}"

# --- Clients ---
app = FastAPI()
jobs_client = JobsClient()

@app.post("/")
async def handle_event(request: Request):
    """
    Receives a CloudEvent from Eventarc (Pub/Sub), parses it, and launches
    a Cloud Run Job, passing the Pub/Sub message payload as an environment variable.
    """
    try:
        event = await request.json()
    except Exception as e:
        print(f"[ERROR] Failed to decode incoming event as JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON event")

    # Pub/Sub wrapper: event may contain "message":{"data": "..."} per Cloud Event
    message = event.get("message") or event.get("data") or event
    data_b64 = None
    if isinstance(message, dict):
        data_b64 = message.get("data")
    elif isinstance(event, dict) and "message" in event:
        data_b64 = event["message"].get("data")
    else:
        # fallback: maybe the entire event is the message
        data_b64 = None

    if not data_b64:
        print("[ERROR] Event does not contain message.data; event body:")
        print(json.dumps(event) if isinstance(event, dict) else str(event))
        raise HTTPException(status_code=400, detail="Missing message data")

    try:
        message_data_str = base64.b64decode(data_b64).decode("utf-8")
    except Exception as e:
        print(f"[ERROR] Failed to base64-decode message.data: {e}")
        raise HTTPException(status_code=400, detail="Malformed message data (base64)")

    # Provide some basic safety: limit payload size for env var
    if len(message_data_str) > 200 * 1024:  # 200 KB guard
        print("[WARN] TASK_PAYLOAD is large; truncating to 200KB for job env var.")
        message_data_str = message_data_str[:200 * 1024]

    # Build env override for the job container
    env_var = EnvVar(name="TASK_PAYLOAD", value=message_data_str)

    # Optionally forward other env flags (e.g. ALLOW_CLONE_FALLBACK) from this service environment
    extra_env = []
    allow_clone = os.environ.get("ALLOW_CLONE_FALLBACK")
    if allow_clone is not None:
        extra_env.append(EnvVar(name="ALLOW_CLONE_FALLBACK", value=allow_clone))
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        extra_env.append(EnvVar(name="GITHUB_TOKEN", value=github_token))

    try:
        run_job_request = RunJobRequest(
            name=TARGET_JOB_PATH,
            overrides=RunJobRequest.Overrides(
                container_overrides=[
                    RunJobRequest.Overrides.ContainerOverride(
                        env=[env_var] + extra_env
                    )
                ]
            )
        )

        print(f"Starting execution for job: {TARGET_JOB_NAME}...")
        operation = jobs_client.run_job(request=run_job_request)
        print(f"Job execution requested, operation: {operation.operation.name}")
        return Response(status_code=202)

    except Exception as e:
        print(f"[ERROR] Error handling event and launching job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
