ARG PYTHON_BASE_IMAGE_VERSION=3.13-slim
FROM crakscloudmgmtprd01.azurecr.io/python:${PYTHON_BASE_IMAGE_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Can be overridden in build args if needed
ARG UID=1001
ARG GID=1001

# Create a non-root user and group with specific UID and GID
RUN groupadd --system --gid ${GID} kvp && useradd --system --create-home --uid ${UID} --gid ${GID} --shell /bin/bash kvpuser
WORKDIR /app

# Ensure pip is up-to-date before install
RUN python -m ensurepip && pip install --upgrade pip

# Copy requirements and install python dependency modules only
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application file
COPY . /app

# Change ownership to the non-root user
RUN chown -R kvpuser:kvp /app
USER kvpuser

# Command to start the controller
ENTRYPOINT ["kopf", "run", "controller.py", "--standalone"]
