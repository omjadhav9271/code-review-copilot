# Use the official lightweight Python image.
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy local code to the container image.
COPY requirements.txt .

# Install production dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY main.py .

# Get the port from the environment variable
ENV PORT 8080

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
