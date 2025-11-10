import os
import base64
from fastapi import FastAPI, Request, HTTPException, Response
import uvicorn
from google.cloud.run_v2 import JobsClient, RunJobRequest, EnvVar

# --- Configuration ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
TARGET_JOB_NAME = os.environ.get("TARGET_JOB_NAME", "security-specialist")
TARGET_JOB_PATH = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/jobs/{TARGET_JOB_NAME}"

# --- Clients ---
app = FastAPI()
jobs_client = JobsClient()

@app.post("/")
async def handle_event(request: Request):
    """
    Receives a CloudEvent from Eventarc, parses it, and launches
    the security-specialist Cloud Run Job, passing the payload as an environment variable.
    """
    try:
        # 1. Parse the CloudEvent
        event = await request.json()
        
        # 2. Extract and decode the Pub/Sub message data
        message_data_str = base64.b64decode(event["message"]["data"]).decode("utf-8")
        
        # 3. Define environment variable override for the task payload
        task_payload_env_var = EnvVar(name="TASK_PAYLOAD", value=message_data_str)

        # --- START OF FIX ---
        # Forward the GITHUB_TOKEN from this service to the job
        extra_env = []
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token:
            extra_env.append(EnvVar(name="GITHUB_TOKEN", value=github_token))
        else:
            print("[WARN] GITHUB_TOKEN not set in security-specialist-executor service, job may fail if analyzing private repos.")
        # --- END OF FIX ---

        # 4. Construct the job run request with overrides
        run_job_request = RunJobRequest(
            name=TARGET_JOB_PATH,
            overrides=RunJobRequest.Overrides(
                container_overrides=[
                    RunJobRequest.Overrides.ContainerOverride(
                        env=[task_payload_env_var] + extra_env # <--- MODIFIED: Added extra_env
                    )
                ]
            )
        )

        # 5. Start the job execution
        print(f"Starting execution for job: {TARGET_JOB_NAME}...")
        operation = jobs_client.run_job(request=run_job_request)
        print(f"Job execution requested, operation: {operation.operation.name}")

        return Response(status_code=202)  # 202 Accepted

    except Exception as e:
        print(f"Error handling event: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)