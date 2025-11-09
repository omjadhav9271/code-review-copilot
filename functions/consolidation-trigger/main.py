# functions/consolidation-trigger/main.py
import os
import json
from google.cloud import pubsub_v1
from google.cloud import firestore

# --- Configuration ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
CONSOLIDATION_TOPIC_ID = "consolidation-tasks"

# --- Clients ---
publisher = pubsub_v1.PublisherClient()
db = firestore.Client()
consolidation_topic_path = publisher.topic_path(GCP_PROJECT_ID, CONSOLIDATION_TOPIC_ID)

# This is a CloudEvent function, triggered by Firestore
def check_completion(cloud_event, context):
    """
    Triggered by any update to a document in the 'reviews' collection.
    Checks if all tasks are complete and triggers consolidation.
    """
    # Get the document path from the event
    resource_string = context.resource
    doc_path = resource_string.split('/documents/')[1].replace('"', '')
    doc_ref = db.document(doc_path)
    
    print(f"Function triggered by update to: {doc_path}")

    # Read the document's data
    # We use a transaction to prevent race conditions
    transaction = db.transaction()
    
    @firestore.transactional
    def trigger_consolidation(transaction, doc_ref):
        doc_snapshot = doc_ref.get(transaction=transaction)
        
        if not doc_snapshot.exists:
            print("Document no longer exists.")
            return

        data = doc_snapshot.to_dict()

        tasks_completed = data.get("tasks_completed", 0)
        total_tasks = data.get("total_tasks", -1) # Default to -1 to avoid 0==0
        status = data.get("status", "")
        
        # --- THE CORRECTED LOGIC ---
        if tasks_completed >= total_tasks and status == "pending":
            print(f"All {total_tasks} tasks complete for {doc_ref.id}. Triggering consolidation.")
            
            # 1. Lock the document to prevent re-triggering
            transaction.update(doc_ref, {"status": "consolidating"})
            
            # --- START OF FIX ---
            # Create a JSON-safe dictionary.
            # This explicitly omits the 'created_at' (datetime) field
            # and only includes what the consolidator needs.
            safe_data = {
                "quality_status": data.get("quality_status"),
                "quality_analysis_results": data.get("quality_analysis_results", []),
                "quality_error": data.get("quality_error"),

                "security_status": data.get("security_status"),
                "security_analysis_results": data.get("security_analysis_results", []),
                "security_error": data.get("security_error"),

                "docs_status": data.get("docs_status"),
                "docs_analysis_results": data.get("docs_analysis_results", []),
                "docs_error": data.get("docs_error"),
            }
            
            # 2. Publish the new safe_data payload
            message_data = json.dumps({
                "review_id": doc_ref.id,
                "pr_info": data.get("pr_info"), # pr_info is already a JSON-safe map
                "full_data": safe_data         # <--- FIXED
            }).encode("utf-8")
            # --- END OF FIX ---
            
            publisher.publish(consolidation_topic_path, data=message_data)
        else:
            print(f"No action needed. (Completed: {tasks_completed}/{total_tasks}, Status: {status})")

    try:
        trigger_consolidation(transaction, doc_ref)
    except Exception as e:
        print(f"Error in consolidation trigger: {e}")