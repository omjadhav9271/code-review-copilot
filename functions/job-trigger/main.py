import os
import json
from google.cloud.run_v2 import JobsClient
from google.api_core.exceptions import GoogleAPICallError

# Configuration from environment variables
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
TARGET_JOB_NAME = os.environ.get("TARGET_JOB_NAME", "quality-analyst")

# Initialize the client to interact with the Cloud Run Jobs API
jobs_client = JobsClient()

def trigger_cloud_run_job(cloud_event, context):
    """
    Cloud Function entry point. Triggered by a Pub/Sub message.
    Invokes a Cloud Run Job.
    """
    print(f"Received event: {context.event_id}")

    # Construct the full job name path
    job_path = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/jobs/{TARGET_JOB_NAME}"

    try:
        # Start a new execution of the job
        operation = jobs_client.run_job(name=job_path)
        print(f"Starting execution for job: {TARGET_JOB_NAME}. Waiting for it to complete...")

        # The .result() call blocks until the job is done. 
        # For a "fire-and-forget" approach, you can just remove this line.
        response = operation.result() 

        print(f"Job execution completed successfully. Response: {response}")

    except GoogleAPICallError as e:
        print(f"Error invoking Cloud Run Job: {e}")
        # Re-raise the exception to signal failure to Cloud Functions
        raise