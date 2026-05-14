# Use lightweight Debian slim on ARM64 for OpenCV compatibility
FROM python:3.11-slim

# Add a non-root user
RUN adduser --disabled-password --gecos "" scraperuser

# Set working directory
WORKDIR /app

# Install system dependencies for OpenCV
RUN apt-get update && apt-get install -y libglib2.0-0 && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY convert_to_png.py .

# Create downloads, processed and data directories and adjust permissions
RUN mkdir -p /app/downloads /app/processed /app/data && chown -R scraperuser:scraperuser /app/downloads /app/processed /app/data

# Switch to non-root user
USER scraperuser

# Command to run the application
CMD ["python", "main.py"]
