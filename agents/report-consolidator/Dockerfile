# Use the official lightweight Python image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Install git (required by GitPython) and clean up apt cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip to latest version
RUN python -m pip install --upgrade pip

# Copy dependency list
COPY requirements.txt .

# Install production dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY main.py .

# Run the script
CMD ["python", "main.py"]
