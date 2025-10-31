import os
import base64
import json
from google.cloud.run_v2 import JobsClient, RunJobRequest, EnvVar

# --- Configuration ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
TARGET_JOB_NAME = os.environ.get("TARGET_JOB_NAME", "quality-analyst")
TARGET_JOB_PATH = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/jobs/{TARGET_JOB_NAME}"

# Optional tokens & flags
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ALLOW_CLONE_FALLBACK = os.environ.get("ALLOW_CLONE_FALLBACK", "false")

# --- Initialize Cloud Run client ---
jobs_client = JobsClient()


def trigger_job_from_pubsub(event, context):
    """
    Cloud Function entrypoint for Pub/Sub → Cloud Run Job trigger.
    Decodes the Pub/Sub message, attaches it as TASK_PAYLOAD,
    and optionally forwards credentials for GitHub API access.
    """
    print(f"Triggered by Pub/Sub message: {context.event_id}")

    # Extract and decode the message
    try:
        data_b64 = event.get("data")
        if not data_b64:
            print("[ERROR] Pub/Sub event missing 'data' field.")
            return "Missing message data"

        message_data_str = base64.b64decode(data_b64).decode("utf-8")
        payload_preview = message_data_str[:200].replace("\n", " ")
        print(f"[INFO] TASK_PAYLOAD preview: {payload_preview}...")
    except Exception as e:
        print(f"[ERROR] Failed to decode Pub/Sub message: {e}")
        return f"Error decoding message: {e}"

    # Prevent environment variable overflow
    if len(message_data_str) > 200 * 1024:
        print("[WARN] TASK_PAYLOAD too large; truncating to 200 KB.")
        message_data_str = message_data_str[:200 * 1024]

    # Prepare environment variables for the Cloud Run Job container
    env_vars = [
        EnvVar(name="TASK_PAYLOAD", value=message_data_str),
        EnvVar(name="ALLOW_CLONE_FALLBACK", value=str(ALLOW_CLONE_FALLBACK)),
    ]
    if GITHUB_TOKEN:
        env_vars.append(EnvVar(name="GITHUB_TOKEN", value=GITHUB_TOKEN))

    # Build and trigger the job execution
    try:
        run_job_request = RunJobRequest(
            name=TARGET_JOB_PATH,
            overrides=RunJobRequest.Overrides(
                container_overrides=[
                    RunJobRequest.Overrides.ContainerOverride(env=env_vars)
                ]
            ),
        )

        print(f"[INFO] Starting Cloud Run Job: {TARGET_JOB_NAME} in {GCP_REGION}")
        operation = jobs_client.run_job(request=run_job_request)
        print(f"[INFO] Job started. Operation name: {operation.operation.name}")

    except Exception as e:
        print(f"[ERROR] Failed to start job: {e}")
        return f"Error triggering job: {e}"

    return "Job execution initiated successfully."


# For Cloud Run adapter compatibility (if needed)
def main(request):
    """
    Optional HTTP wrapper for Cloud Run deployment.
    Accepts the same Pub/Sub-style event JSON via POST.
    """
    try:
        body = request.get_json(silent=True)
        if not body:
            return ("Invalid request: expected JSON body", 400)
        message = body.get("message", {})
        data_b64 = message.get("data")
        if not data_b64:
            return ("Missing message.data in request", 400)

        # Simulate Cloud Function trigger
        response = trigger_job_from_pubsub({"data": data_b64}, context=type("c", (), {"event_id": "http"})())
        return (response, 202)
    except Exception as e:
        print(f"[ERROR] Failed to handle HTTP request: {e}")
        return (str(e), 500)
