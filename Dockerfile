# Use the official Python image
FROM python:3.12-slim

# Set environment variables to non-interactive
ENV DEBIAN_FRONTEND=noninteractive

# Set the working directory
WORKDIR /usr/src/app

# Copy the current directory contents into the container at /usr/src/app
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Make the Python script executable
RUN chmod +x main.py

# Define the command to run the application
CMD ["python", "./main.py"]