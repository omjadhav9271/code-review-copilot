# GitHub Code Review Copilot

A serverless, multi-agent AI system that automatically reviews GitHub pull requests for code quality, security vulnerabilities, and documentation gaps.

This is a **headless B2D (Business-to-Developer) tool**. Its "frontend" is the GitHub pull request interface itself, posting synthesized reports as PR comments to meet developers exactly where they work.



---

## 🏛️ System Architecture

This project is a resilient, event-driven system built on a serverless, multi-agent "fan-out, fan-in" architecture.

1.  **Orchestration (Fan-Out):** A GitHub webhook (`pull_request`) triggers the public-facing `pr-orchestrator` service. This service validates the request, creates a state-tracking document in Firestore (`status: "pending"`, `total_tasks: 3`), and publishes three distinct "task" messages to separate Pub/Sub topics.
2.  **Specialist Agents (Execution):** Three private `Executor` services, each listening to a single topic, are triggered in parallel. Each executor service's sole job is to launch its corresponding `Agent` (a Cloud Run Job), passing the task payload.
3.  **Agent Work:** The three agents (`quality-analyst`, `security-specialist`, `doc-drafter`) run in parallel:
    * They fetch PR data (using an API-first, Git-fallback strategy).
    * They execute highly-specialized prompts against the Vertex AI Gemini API.
    * They write their unique results (e.g., `quality_analysis_results`) back to the Firestore document and atomically increment the `tasks_completed` counter.
4.  **Consolidation (Fan-In):** A 2nd Gen Cloud Function (`consolidation-trigger`) listens for all updates to the Firestore document. When it detects `tasks_completed >= total_tasks` and `status == "pending"`, it "locks" the document by setting `status: "consolidating"` and publishes a final message to the `consolidation-tasks` topic.
5.  **Final Report:** The `report-consolidator-executor` service is triggered by this message, launching the final `report-consolidator` job. This job reads all agent reports from Firestore, uses Gemini to synthesize them into a single Markdown comment, and posts it to the GitHub PR. Finally, it sets the document `status: "complete"`.



---

## 🔩 Component Breakdown


The system is composed of 10 distinct microservices and jobs, each with a single responsibility.

| Component | GCP Service | Source Code | Purpose |
| :--- | :--- | :--- | :--- |
| **`pr-orchestrator`** | Cloud Run Service | `services/pr-orchestrator` | Public webhook entry point. Validates & starts the review. |
| **`quality-analyst-executor`** | Cloud Run Service | `services/quality-analyst-executor` | Private service. Listens to `code-review-tasks` topic, starts the quality job. |
| **`quality-analyst`** | Cloud Run Job | `agents/quality-analyst` | Runs Gemini analysis for code smells, bugs, & best practices. |
| **`security-specialist-executor`** | Cloud Run Service | `services/security-specialist-executor`| Private service. Listens to `security-review-tasks` topic, starts the security job. |
| **`security-specialist`** | Cloud Run Job | `agents/security-specialist` | Runs Gemini analysis for vulnerabilities & sensitive data. |
| **`doc-drafter-executor`** | Cloud Run Service | `services/doc-drafter-executor` | Private service. Listens to `docs-review-tasks` topic, starts the docs job. |
| **`doc-drafter`** | Cloud Run Job | `agents/doc-drafter` | Runs Gemini analysis for missing/stale documentation. |
| **`consolidation-trigger`** | Cloud Function (Gen2) | `functions/consolidation-trigger` | Listens to Firestore. Triggers consolidation when all tasks are done. |
| **`report-consolidator-executor`** | Cloud Run Service | `services/report-consolidator-executor`| Private service. Listens to `consolidation-tasks` topic, starts the report job. |
| **`report-consolidator`** | Cloud Run Job | `agents/report-consolidator` | Runs Gemini to synthesize all reports & post the final comment to GitHub. |

---

## ✨ Key Features & Design Decisions

This project was built with a focus on resilience, scalability, and maintainability.

* **Multi-Agent System:** Instead of a single complex prompt, the system uses a team of specialists. This improves maintainability and allows for fine-tuned logic (e.g., the `security-specialist` checks `.yaml` and `.json` files, while the `quality-analyst` only checks `.py`, `.js`, and `.go`).
* **Resilient & Atomic State Management:** The system is designed to survive race conditions and duplicate events.
    * **SHA-Checking:** All agents perform atomic Firestore writes that are conditional on the `head_sha`. If a new commit is pushed, in-flight jobs for the old commit will fail to write, correctly orphaning their stale results.
    * **`>=` Logic:** The `consolidation-trigger` uses `tasks_completed >= total_tasks` (not `==`) to ensure that even if a duplicate task runs (e.g., `4/3`), the consolidation will still trigger.
    * **State Locking:** The trigger immediately sets the `status` to `"consolidating"` within its transaction to guarantee it only runs once per review.
* **Robust Error Handling:**
    * **Exponential Backoff:** All Vertex AI API calls are wrapped in a `generate_content_with_retry` helper, making the system resilient to `429 (Resource Exhausted)` and `503 (Service Unavailable)` errors.
    * **Debuggable Errors:** Each agent writes its errors to a specific field (e.g., `quality_error`). The final `report-consolidator` is programmed to read these fields and explicitly state in its final report which agents failed and why.
* **Secure by Design:**
    * **Single Public Endpoint:** The `pr-orchestrator` is the *only* service exposed to the internet. It validates all incoming requests using a `GITHUB_WEBHOOK_SECRET`.
    * **Private Internal Services:** All other services are private (`--no-allow-unauthenticated`).
    * **Least Privilege IAM:** The entire system runs using a single, dedicated service account (`...-compute@...`) with the minimal required roles:
        * `Cloud Datastore User` (for Firestore)
        * `Pub/Sub Publisher` (for publishing tasks)
        * `Cloud Run Invoker` (for Eventarc/Executors to start other services/jobs)
        * `iam.serviceAccountUser` (for the Eventarc agent to impersonate the service account)
* **Efficient Code Fetching:** Agents use an **API-first, Git-fallback** strategy. They first attempt to fetch file contents via the lightweight GitHub REST API. If a file is too large or the API fails, they fall back to performing a shallow `git clone` to ensure the review is always completed.

---


## 🛠️ Tech Stack

* **Platform:** Google Cloud Platform
* **Compute:** Cloud Run (Services & Jobs), Cloud Functions (Gen 2)
* **Messaging:** Eventarc, Pub/Sub
* **Database:** Firestore (Datastore Mode)
* **AI:** Vertex AI (Gemini 2.5 Flash & Pro)
* **Framework:** FastAPI (for all services)
* **Tools:** GitHub API, GitPython, Docker

---

## 🚀 Setup & Deployment

1.  **Prerequisites:**
    * A Google Cloud Project with billing enabled.
    * A GitHub Personal Access Token (PAT) with `repo` scope.
    * `gcloud` SDK installed and authenticated.
2.  **Create GCP Resources:**
    * Enable all required APIs (Cloud Run, Eventarc, Firestore, Vertex AI, IAM, Pub/Sub, Cloud Build).
    * Create the four Pub/Sub topics: `code-review-tasks`, `security-review-tasks`, `docs-review-tasks`, `consolidation-tasks`.
3.  **Deploy Services & Jobs:**
    * `cd` into each service/agent directory and run the corresponding `gcloud` deploy command (as provided in the project files).
    * **Critical:** Ensure all `GITHUB_TOKEN` and other environment variables are set correctly during deployment, especially on the executor services.
4.  **Configure IAM:**
    * Ensure your primary service account (e.g., `...-compute@...`) has the following roles: `Cloud Datastore User`, `Pub/Sub Publisher`, `Cloud Run Invoker`, and `Service Account User`.
    * Grant the Google-managed Eventarc agent (`service-...@gcp-sa-eventarc.iam.gserviceaccount.com`) the `roles/iam.serviceAccountUser` role on your primary service account.
5.  **Create Eventarc Triggers:**
    * Deploy the `consolidation-trigger` Cloud Function (as shown in the deployment commands). This will automatically create the Firestore trigger.
    * Manually create the Eventarc triggers to link the three agent Pub/Sub topics (e.g., `code-review-tasks`) to their corresponding executor services (e.g., `quality-analyst-executor`).
    * Manually create the final Eventarc trigger to link the `consolidation-tasks` topic to the `report-consolidator-executor` service.
6.  **Configure GitHub Webhook:**
    * In your GitHub repo, create a new webhook.
    * Set the **Payload URL** to the public URL of your `pr-orchestrator` service.
    * Set the **Content type** to `application/json`.
    * Set the **Secret** to match the `GITHUB_WEBHOOK_SECRET` you deployed with.
    * Subscribe to "Pull Request" events.