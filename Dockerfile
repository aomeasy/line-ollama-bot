# Use an official Python runtime as a parent image
FROM python:3.9-slim


# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's source code into the container
COPY . .

# Expose port 5000 (Flask's default port)
EXPOSE 5000

# Run the command to start the application using Gunicorn
# Gunicorn is a production-grade WSGI HTTP Server.
# The command assumes your Flask app is named 'app' in 'main.py'
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "main:app"]


CMD ["python", "main.py"]
